# FortiADC Lab op Proxmox

Een volledig virtueel demo-lab voor **FortiADC**: server load balancing, health checks, persistence, SSL
offloading, content routing, WAF en GSLB over twee datacenters, **elk met een eigen FortiADC**. De twee
FortiADC's vormen samen de GSLB: beide zijn authoritative voor het GSLB-subdomein en synchroniseren elkaars
virtual servers (auto-sync), zodat ook het wegvallen van een compleet datacenter, inclusief zijn FortiADC,
opgevangen wordt. Een controller-container met web UI rolt
alles uit en heeft daarna een **demo-cockpit** waarmee je met één klik storingen veroorzaakt, verkeer
genereert en aanvallen uitvoert. Zelfde opzet als het [FortiGate OT Lab](https://github.com/kay-arne/fortigate-ot-lab).

```
  presentator ── vmbr0 ──┬── port1 FAD-AMS ── port3 ── vmbr810 Datacenter Amsterdam (realservers)
   (management,          │        │
    GSLB-sync)           └── port1 FAD-FRA ── port3 ── vmbr820 Datacenter Frankfurt (realservers)
                                  │  │
                          port2 ──┘  └── port2
                                  │
             vmbr800 "internet": desktop, client, DNS · VIP's van beide · GSLB-DNS ns1 (AMS) + ns2 (FRA)
```

| Component | Type | Inhoud |
|---|---|---|
| `adclab-controller` | LXC (vmbr0) | web UI, Proxmox-orchestratie, lab-CA, FortiADC-provisioning, demo-cockpit |
| `adclab-fad-ams`, `adclab-fad-fra` | VM | FortiADC-VM per datacenter (KVM-image, elk een eigen licentie): SLB/WAF voor het eigen DC, GSLB samen |
| `adclab-desktop` | VM | Ubuntu 24.04 Desktop + Firefox (deb), vertrouwt de lab-CA, gebruikt de lab-DNS |
| `adclab-dns` | LXC | BIND9: zone `adclab.internal`, delegeert `gslb.adclab.internal` aan beide FortiADC's |
| `adclab-client` | LXC | verkeers- en aanvalsgenerator voor het cockpit (`labclient`) |
| `adclab-shop-*`, `-portal-*`, `-api-*` | LXC | eigen demo-webapp (`labapp/`), per datacenter, schakelbare health |
| `adclab-juice-ams`, `adclab-dvwa-fra` | LXC + Docker | OWASP Juice Shop (AMS) en DVWA (FRA) als WAF-doelwit |

## Installatie

Op de Proxmox host (als root):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/kay-arne/fortiadc-lab/main/install.sh)"
```

Net als bij de Proxmox community scripts kies je **Standaard** of **Geavanceerd** (storage, bridge, VLAN,
DHCP/statisch IP, VMID-reeks, aparte host-gebruiker). Onbeheerd kan ook:

```bash
ADCLAB_UNATTENDED=1 CT_BRIDGE=vmbr0 CT_IP=192.168.1.240/24 CT_GW=192.168.1.1 \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/kay-arne/fortiadc-lab/main/install.sh)"
```

Zie de kop van [install.sh](install.sh) voor alle variabelen. De installer maakt de controller-LXC, koppelt
hem via SSH aan de host (standaard een aparte gebruiker `adclab` met sudo voor alleen `qm`, `pct`, `pvesh`
en het plaatsen van twee config-ISO's; intrekken met `adclab-revoke`) en print de URL en het wachtwoord van
de web UI. Draai je de oneliner opnieuw, dan biedt hij **Bijwerken** aan.

## Gebruik

1. **Setup**: upload de twee images uit de FortiADC KVM-zip (`boot.qcow2` en `data.qcow2`, één set voor
   beide FortiADC's), stel per FortiADC een
   management-IP in plus gateway, DNS en het admin-wachtwoord, en kies per FortiADC de licentie (`.lic` of
   FortiFlex-token).
2. **Lab → Volledig lab uitrollen**. De controller:
   1. maakt de bridges `vmbr800`/`810`/`820` (zonder fysieke poort, zonder host-IP);
   2. maakt per datacenter een FortiADC-VM volgens Fortinets KVM-installatiegids: `boot.qcow2` als eerste
      en `data.qcow2` als tweede VirtIO-disk (vergroot tot 30 GB voor logs), VirtIO-NIC's en 8 GB RAM;
   3. bouwt één Debian 12-LXC-template (labapp, BIND, Docker) en kloont daaruit alle containers;
   4. haalt de Docker-images voor Juice Shop en DVWA eenmalig op (container kort aan de uplink);
   5. maakt de desktop-VM uit het officiële Ubuntu cloud image (SHA256 gecontroleerd) met cloud-init;
   6. zet de configuratie. Day-0 gaat via de seriële console (`qm terminal`): inloggen in de fabrieksstand,
      wachtwoord zetten en management/interfaces/route/DNS intypen; daarna de licentie (FortiFlex-token of
      `.lic` via REST, de FortiADC herstart). Dan per FortiADC certificaten (REST) en SLB, content routing en WAF voor zijn
      eigen datacenter (CLI via SSH); daarna op beide dezelfde GSLB-config (data centers, servers, pools,
      hosts, DNS-policy). Die volgorde is nodig omdat auto-sync de virtual servers van de andere FortiADC
      ophaalt.
3. **Demo-cockpit**: knoppen per use case; zie [docs/demo](docs/demo/00-voorbereiding.md).
4. **Toegang & scripts**: logins, de lab-CA om te downloaden, alle namen en de demo-scripts.

De desktop installeert zichzelf (Ubuntu Desktop + Firefox) en is na ~15-30 minuten klaar; de Lab-tab
toont de cloud-init-status. De desktop is je weg het lab in: zijn eerste NIC hangt aan dezelfde
bridge/VLAN als port1 van de FortiADC's (je management-LAN, DHCP of een vast IP uit Setup stap 6), de
tweede in het lab. Neem hem over met **RDP** (Windows: *Extern bureaublad*, macOS: *Windows App*)
op zijn uplink-adres: de Toegang-tab haalt dat adres op via de guest agent en toont gebruiker en
wachtwoord. De Proxmox-console werkt ook. Een bestaande desktop krijgt nieuwe afstellingen (RDP, geen
welkomstwizard) met **Toegang → Desktop bijwerken en herstarten**, zonder herinstallatie.

## Praktijkkennis

[docs/fortiadc-praktijk.md](docs/fortiadc-praktijk.md) bundelt wat we op FortiADC 8.0.4 tegenkwamen en hoe je
het goed configureert: uitrol op Proxmox (twee disks, day-0 via de console), FortiFlex, tijdzone,
CLI-verschillen met de documentatie, GSLB met twee FortiADC's (`DEFAULT_DNS_SERVER`, auto-sync), de
valkuilen van de global DNS, en een diagnosetabel voor `dig`. Bruikbaar los van het lab, ook bij klanten.

## Demo-scripts

| Script | Use case |
|---|---|
| [0. Voorbereiding](docs/demo/00-voorbereiding.md) | schermindeling, checklist, naamgeving, troubleshooting |
| [1. Overzicht](docs/demo/01-overzicht.md) | het lab en het verhaal (Polder Retail) |
| [2. Load balancing & persistence](docs/demo/02-load-balancing.md) | VS, pool, LB-methodes, insert-cookie |
| [3. Health checks](docs/demo/03-health-checks.md) | applicatiefout, crash, herstel |
| [4. SSL offloading](docs/demo/04-ssl-offloading.md) | certificaten, TLS 1.2+, HTTP → HTTPS |
| [5. Content routing](docs/demo/05-content-routing.md) | één VIP, routeren op host en pad |
| [6. WAF](docs/demo/06-waf.md) | monitoren vs. blokkeren, SQLi/XSS, exceptions |
| [7. GSLB](docs/demo/07-gslb.md) | twee FortiADC's, DNS-delegatie, auto-sync, failover incl. uitval van een FortiADC |
| [8. GSLB + content routing](docs/demo/08-gslb-content-routing.md) | Generic Host, health check per FQDN |
| [9. Zichtbaarheid](docs/demo/09-zichtbaarheid.md) | FortiView, traffic- en WAF-logs |

## Wat zit erin

| Bestand | Inhoud |
|---|---|
| `lab/topology.yaml` | **één bron**: segmenten, containers, apps, virtual servers, content routes, GSLB, desktop |
| `controller/app/adcconfig.py` | genereert de CLI per FortiADC uit de topologie (ook te zien via *Config bekijken*) |
| `controller/app/fortiadc.py` | day-0 via de seriële console, licentie, SSH-CLI met foutdetectie per regel, REST |
| `controller/app/pki.py` | lab-CA (Name Constraints) en servercertificaten |
| `controller/app/deployer.py` | Proxmox-orchestratie, BIND-zone, Docker-apps, Ubuntu-desktop (cloud-init) |
| `controller/app/demo.py` | demo-cockpit |
| `labapp/` | demo-webapp (`webapp.py`), `labctl` (health/vertraging), `labclient` (verkeer, DNS, aanvallen, TLS) |
| `docs/demo/` | demo-scripts |

Een applicatie toevoegen = in `lab/topology.yaml` een node, een `apps`-regel en een `virtual_servers`-regel
toevoegen; DNS, certificaat, pool, health check, VS en (optioneel) GSLB volgen vanzelf.

## Best practices in het ontwerp

- **Isolatie**: lab-bridges zonder fysieke poort; de controller bedient containers out-of-band (`pct exec`).
  Alleen FortiADC-management en de desktop-uplink hangen aan je netwerk.
- **Minimale rechten op de host**: aparte gebruiker, sudo voor alleen de nodige commando's, sleutel aan
  het controller-IP gebonden (bij statisch IP), intrekbaar met `adclab-revoke`.
- **Private PKI**: root-CA met *Name Constraints* (alleen `adclab.internal`), certificaten van 397 dagen
  met SAN's en `serverAuth`, sleutels alleen in de controller (`data/pki`, 0600). TLS 1.2+ op de VIP's.
- **GSLB zoals in productie**: per datacenter een eigen FortiADC; beide authoritative (twee NS-records), dus
  geen single point of failure; gelijkwaardige GSLB-config op beide: de eigen FortiADC via de ingebouwde
  server `DEFAULT_DNS_SERVER`, de andere als *FortiADC-SLB*-server met auto-sync; *Generic Host* met een health
  check per FQDN waar FQDN's één VIP delen (content routing). Hetzelfde certificaat in beide datacenters.
- **DNS**: `.internal` (door ICANN gereserveerd voor privé-gebruik); GSLB via **delegatie van een
  subdomein** met CNAME's vanaf de vriendelijke namen; de desktop gebruikt split DNS (alleen het
  labdomein naar de lab-DNS).
- **Health checks op applicatieniveau** (pad + inhoud), en voor GSLB **per FQDN met Host-header**.
- **Gehardende demo-webapp**: eigen dynamische gebruiker, alleen `CAP_NET_BIND_SERVICE`,
  `ProtectSystem=strict`; geen externe afhankelijkheden.
- **Reproduceerbaar**: vaste image-tags, gecontroleerde downloads (SHA256 Ubuntu-image, fingerprint van
  Mozilla's APT-sleutel), idempotente provisioning (opnieuw toepassen mag altijd).

## Status en aandachtspunten

**Uitgerold op Proxmox met FortiADC-KVM 8.0.4** (twee VM's, FortiFlex): day-0 via de console,
licentie-activatie, certificaten en client-SSL-profielen via REST, en de volledige CLI-config (SLB, content
routing, WAF, GSLB en DNS-policy) zonder geweigerde regels. De Ubuntu desktop komt op met GUI.
**Nog niet functioneel gecontroleerd**: de GSLB-antwoorden en de auto-sync tussen de FortiADC's in een
failover. Aandachtspunten:

- **Disks**: `data.qcow2` wordt na het importeren vergroot (`data_disk_gb` in `lab/topology.yaml`). Gebruikt
  FortiADC de extra ruimte niet, of start hij niet met een vergrote datadisk, zet `data_disk_gb: 0`.
- **Tijdzone**: de FortiADC's krijgen de tijdzone uit `lab.timezone` (standaard Europe/Amsterdam). Het
  zone-nummer zoekt de controller op in de lijst van de FortiADC zelf (`set zone ?`), met 26 (Fortinet-nummer
  voor Amsterdam) als terugval.
- **Geheugen**: 8 GB per FortiADC (Fortinets minimum), dus 16 GB voor beide. Krap op je host? Verlaag
  `memory` in `lab/topology.yaml`; voor een demo werkt 4 GB meestal, maar dat is onder het minimum.
- **Day-0**: een cloud-init config drive (config-drive v2) werkt op Proxmox niet voor FortiADC (getest met
  8.0.4); de controller gebruikt daarom altijd de seriële console. Is het management-IP al bereikbaar
  (bijv. bij opnieuw provisionen), dan slaat hij die stap over.
- **Licentie**: herkend aan het serienummer (`FADV0000000TRIAL` = geen licentie). Een geldige licentie
  wordt dus niet opnieuw geactiveerd bij een volgende provisioning.
- **WAF-objecten**: de namen van de voorgedefinieerde signature- en SQLi/XSS-objecten verschillen per
  versie. De controller leest ze op de FortiADC en kiest het passende niveau (hoog of alleen alert); de log
  toont welke.
- **CLI-syntax per build**: de config is geschreven voor FortiADC 7.4 en later en wordt op 8.0.4 volledig
  geaccepteerd. Wat 8.0 anders wil dan de oudere documentatie: `domain-name` van een GSLB-host met
  afsluitende punt, `load-balance-method weight` (niet `none`) en `heuristic-sql-xss-injection-detect` in het
  WAF-profiel. Voor zulke verschillen heeft de controller een terugvallijst (`CLI_ALTERNATIVES` in
  `fortiadc.py`). Elke geweigerde regel komt als waarschuwing in de log en op de Lab-tab (*Toon*); de rest
  wordt wel toegepast.
- **GSLB-zone en DNS-policy**: de hosts genereren de zone `fqdn_generate_gslb.adclab.internal.`; de
  controller leest die naam terug en hangt hem aan de DNS-policy `DNS-LAB`. Policies matchen van boven naar
  beneden, en de ingebouwde `DEFAULT_DNS_POLICY` (any/any, recursief) staat bovenaan. Daarom schuift de
  controller `DNS-LAB` erboven (`move`); anders beantwoordt de FortiADC GSLB-namen recursief via internet
  (NXDOMAIN met de root-SOA, zonder `aa`-vlag). De global DNS-server zelf (`gds-status`) staat in de
  fabrieksstand uit en wordt aangezet. Op de gegenereerde zone zet de controller een korte TTL
  (`dns.gslb_ttl`, standaard 30 s in plaats van 86400) en als primaire nameserver het eigen port2-adres
  (`ns1`/`ns2`, gelijk aan de delegatie in BIND) in plaats van `defaultprimary` op 127.0.0.1.
- **GSLB-sync tussen de FortiADC's**: elke FortiADC kent zichzelf als de ingebouwde `DEFAULT_DNS_SERVER`
  (127.0.0.1), die de eigen virtual servers synchroniseert; dat werkt op 8.0.4. Een FortiADC-SLB-server naar
  het eigen management-IP bleef leeg en wordt opgeruimd. De andere FortiADC staat erin als `FAD-<dc>` op zijn
  management-IP (TCP 5858, auto-sync). De GSLB-pools verwijzen naar de gesynchroniseerde namen
  (`VS-SHOP-AMS`). De serverregel krijgt `auth_verify` met een gedeelde `auth-key` (`dns.gslb_sync_auth`); op 8.0.4
  eist de luisterende kant (`config global-load-balance setting`) daar nog niets mee, dus scherm poort 5858
  af. Na de provisioning controleert de controller
  of de sync werkt en valt hij zo nodig terug op `none`. Zie [docs/fortiadc-praktijk.md](docs/fortiadc-praktijk.md).
- **REST**: certificaatupload via `/api/upload/certificate_local` en client-SSL-profielen via
  `/api/load_balance_client_ssl_profile`, zoals Fortinets Ansible-collectie het doet.
- **FortiFlex**: na de day-0 geactiveerd met `execute vm-license <token>` (werkt op 8.0.4; de FortiADC
  herstart). Een gelicenseerde FortiADC herken je aan een echt serienummer (zonder licentie:
  `FADV0000000TRIAL`); die wordt bij een volgende provisioning niet opnieuw geactiveerd.
- **Docker in LXC** (Juice Shop, DVWA) vraagt `nesting=1,keyctl=1` en een storage met overlay-ondersteuning
  (ZFS 2.2+/LVM-thin). Linked clones werken op LVM-thin/ZFS; op `dir`-storage valt de controller terug op
  full clones (meer schijfruimte).
- **Desktop**: geen welkomstwizard (Ubuntu Pro, Livepatch, telemetrie) en geen pop-ups van de update- en
  crashmelder (`gnome-initial-setup`, `update-notifier`, `apport` verwijderd). RDP via xrdp (TCP 3389 op de
  uplink) met dezelfde Ubuntu-sessie als op de console. Dezelfde gebruiker kan niet tegelijk op de console
  en via RDP ingelogd zijn; daarom logt de console standaard **niet** automatisch in
  (`desktop.console_autologin` in `lab/topology.yaml`). xrdp gebruikt een self-signed certificaat: je
  RDP-client vraagt daarom eenmalig om het te accepteren.
- De web UI gebruikt HTTP met een loginformulier (gebruiker `admin`, wachtwoord in
  `/opt/adclab/data/ui_password`): zet hem niet buiten je labnetwerk open.

## Bijwerken en afbreken

- **Web UI → Updates**, de installer-oneliner opnieuw (kies *Bijwerken*), of `/opt/adclab/update.sh` in de
  controller. Instellingen, uploads en de lab-CA staan in `data/` en blijven behouden. Wijzigt een update de
  labapp, dan verschijnt op de Lab-tab de knop *Labapp bijwerken*.
- **Lab → Alles afbreken…** verwijdert VM's en containers (optioneel ook template en bridges). De
  controller verwijder je zelf met `pct destroy <id>`, daarna `adclab-revoke` op de host.
