# FortiADC in de praktijk: bevindingen en juiste configuratie

Dit document legt vast wat we bij het bouwen van het FortiADC Lab tegenkwamen: dingen die anders werken dan
de documentatie doet vermoeden, standaardinstellingen die je moet aanpassen, en hoe je problemen snel
vindt. Het is bedoeld voor je eigen uitrollen en voor klantsituaties, los van het lab.

**Getest met:** FortiADC-KVM **v8.0.4** build0136 (GA.F), twee VM's op Proxmox VE, FortiFlex-licenties.
**Datum:** november 2026.

Status per punt:

- ✅ **bevestigd**: op 8.0.4 waargenomen en opgelost;
- 🟡 **toegepast, effect nog niet bevestigd**: de config wordt geaccepteerd, maar het gedrag is nog niet gecontroleerd;
- ❔ **open**: oorzaak of juiste instelling nog onbekend.

Waar de lab-controller het automatisch regelt, staat de plek in de code erbij.

---

## Inhoud

1. [Uitrol op Proxmox / KVM](#1-uitrol-op-proxmox--kvm)
2. [Day-0: eerste configuratie](#2-day-0-eerste-configuratie)
3. [Licentie (FortiFlex)](#3-licentie-fortiflex)
4. [Tijdzone](#4-tijdzone)
5. [CLI-verschillen in 8.0 ten opzichte van de documentatie](#5-cli-verschillen-in-80-ten-opzichte-van-de-documentatie)
6. [GSLB met twee FortiADC's](#6-gslb-met-twee-fortiadcs)
7. [Global DNS: de vier valkuilen](#7-global-dns-de-vier-valkuilen)
8. [GSLB en content routing (gedeelde VIP)](#8-gslb-en-content-routing-gedeelde-vip)
9. [Diagnose: DNS en GSLB stap voor stap](#9-diagnose-dns-en-gslb-stap-voor-stap)
10. [Demo-werkplek (Ubuntu desktop)](#10-demo-werkplek-ubuntu-desktop)
11. [Checklist voor een nieuwe uitrol](#11-checklist-voor-een-nieuwe-uitrol)

---

## 1. Uitrol op Proxmox / KVM

### Twee disk-images ✅

Het KVM-pakket (`FAD_KVM-…-FORTINET.out.kvm.zip`) bevat **twee** images:

| Bestand | Disk | Inhoud |
|---|---|---|
| `boot.qcow2` (~240 MB) | eerste disk, **VirtIO** (`virtio0`), bootdisk | systeem |
| `data.qcow2` (~31 MB) | tweede disk, **VirtIO** (`virtio1`) | logs, rapporten |

Het Fortinet-artikel over Proxmox noemt alleen `boot.qcow2`; de KVM-installatiegids en de OpenStack-gids
zeggen dat de datadisk nodig is. Op Proxmox:

```bash
qm create <vmid> --name fad-ams --ostype l26 --cpu host --cores 2 --memory 8192 \
  --scsihw virtio-scsi-single --serial0 socket \
  --net0 virtio,bridge=vmbr0,firewall=0 --net1 virtio,bridge=<lab-bridge>,firewall=0
qm disk import <vmid> boot.qcow2 <storage>      # -> unused0
qm set <vmid> --virtio0 <storage>:vm-<vmid>-disk-0
qm disk import <vmid> data.qcow2 <storage>      # -> unused0
qm set <vmid> --virtio1 <storage>:vm-<vmid>-disk-1
qm set <vmid> --boot order=virtio0
```

- **Geheugen**: Fortinet noemt minimaal **8 GB** ✅ (draait in het lab met 8 GB per FortiADC).
- **vCPU's**: niet meer dan de licentie toestaat (VM02 = 2 vCPU).
- **NIC's**: VirtIO, Proxmox-firewall uit op de NIC. De volgorde bepaalt port1, port2, …
- **`--serial0 socket`** is nodig voor day-0 via de console (hoofdstuk 2).
- **Datadisk vergroten** (`qm disk resize <vmid> virtio1 30G`) 🟡: de FortiADC start ermee, maar of hij de
  extra ruimte gebruikt, is niet gecontroleerd.

*Lab:* `deployer.deploy_fortiadc()`, `lab/topology.yaml` (`fortiadc:`).

---

## 2. Day-0: eerste configuratie

### Cloud-init config drive werkt niet op Proxmox ✅

FortiADC documenteert *config-drive v2* (`openstack/latest/user_data` = CLI, `openstack/content/0000` =
licentie) voor VMware en OpenStack. Op Proxmox/KVM met 8.0.4 werd zo'n ISO (label `config-2`, als cd-rom)
**niet** opgepakt: na 10 minuten nog steeds geen management-IP.

### Wat wel werkt: de seriële console ✅

Met `--serial0 socket` kun je via `qm terminal <vmid> -iface serial0` inloggen en de CLI intypen
(verlaten met `Ctrl-O`):

- fabrieksstand: gebruiker `admin`, **leeg wachtwoord**;
- daarna vraagt de FortiADC direct om een nieuw wachtwoord (*New Password* / *Confirm Password*);
- de eerste boot duurt een paar minuten; wacht op de `login:`-prompt.

Minimale day-0-config:

```
config system global
    set hostname fad-ams
end
config system interface
    edit "port1"
        set mode static
        set ip 10.10.30.211/24
        set allowaccess https ssh ping http
    next
end
config router static
    edit 1
        set gateway 10.10.30.1
    next
end
config system dns
    set primary 10.10.30.1
end
```

*Lab:* `fortiadc.console_bootstrap()` doet dit automatisch, maar alleen als het management-IP nog niet
bereikbaar is.

---

## 3. Licentie (FortiFlex)

| Bevinding | Status |
|---|---|
| `execute vm-license <token>` activeert een FortiFlex-token; de FortiADC vraagt bevestiging (`y`) en **herstart**. | ✅ |
| `get system status` toont in 8.0 **geen** `License:`-regel. | ✅ |
| Een FortiADC zonder licentie heeft serienummer **`FADV0000000TRIAL`**; na activatie het echte serienummer (bijv. `FDVMMLTM26001228`). Dat is de betrouwbare check. | ✅ |
| Opnieuw activeren op een al gelicenseerde FortiADC geeft **`Failed to install VM license`** en **geen** herstart. | ✅ |

Gevolg voor automatisering: controleer het serienummer voordat je activeert. Wacht na activatie eerst tot de
FortiADC onbereikbaar wordt, en daarna tot hij terug is; anders zie je nog net de oude sessie.

*Lab:* `fortiadc.license_status()`, `ensure_license()`, `wait_reboot()`.

---

## 4. Tijdzone ✅ / 🟡

De fabrieksstand is **zone 4 (Pacific)**. FortiADC gebruikt een **nummer**, geen naam:

```
config system time manual
    set zone 26
    set daylight-saving-time enable
end
```

- 26 is in de Fortinet-lijst *(GMT+1:00) Amsterdam, Berlin, Bern, Rome, Stockholm, Vienna*. 🟡 Controleer het
  nummer met `set zone ?` binnen `config system time manual`, want de documentatie geeft de lijst niet.
- Zet ook NTP (`config system time ntp`), zodat logs van beide FortiADC's synchroon lopen.

*Lab:* `fortiadc.set_timezone()` zoekt de stad op in de lijst van de FortiADC zelf, met 26 als terugval.

---

## 5. CLI-verschillen in 8.0 ten opzichte van de documentatie

De CLI-reference (7.4/7.6) en het gedrag van 8.0.4 verschillen op een paar punten. Een geweigerde regel geeft
`Command fail. Return code is -284 (CLI parsing error.)`.

| Onderwerp | Documentatie | Werkt op 8.0.4 | Status |
|---|---|---|---|
| WAF-profiel, heuristische SQLi/XSS | `set heuristic-sql-xss-injection-detection <obj>` | **`set heuristic-sql-xss-injection-detect <obj>`** (zo staat het ook in het voorbeeld van de 8.0-reference) | ✅ |
| GSLB-host, domein | `set domain-name example.com` | **`set domain-name example.com.`** (met afsluitende punt; anders `-3006 Invalid domain name` bij `next`) | ✅ |
| GSLB-host, gewogen (actief/actief) | `set load-balance-method none` | **`set load-balance-method weight`** | ✅ |
| GSLB-host, actief/standby | `global availability` | `set load-balance-method global-availability` | ✅ |

Wat wél zoals gedocumenteerd werkt (✅ geaccepteerd op 8.0.4): health checks met `type https`,
`http-version http_1.1` en `hostname`; `set location` op data centers; `listen-on-all-interface`; pools met
`config pool_member`; content routing met `set object http-host-header | http-request-url`; virtual
servers met `load-balance-*`, `client-ssl-profile`, `waf-profile`, `http2https`.

Voorgedefinieerde WAF-objecten op 8.0.4 (zowel *web-attack-signature* als *heuristic-sql-xss-injection-detection*):
`High-Level-Security`, `Medium-Level-Security`, `Alert-Only`.

*Lab:* `CLI_ALTERNATIVES` in `fortiadc.py`. Weigert de FortiADC een regel, dan probeert de controller de
andere schrijfwijze, zodat dezelfde config op 7.x en 8.0 werkt.

---

## 6. GSLB met twee FortiADC's

Opzet: elk datacenter heeft een eigen FortiADC. Beide zijn authoritative voor het GSLB-subdomein en weten
via auto-sync welke virtual servers de ander heeft en of die gezond zijn.

### De eigen FortiADC is `DEFAULT_DNS_SERVER` ✅

Elke FortiADC heeft een **ingebouwde** GSLB-server `DEFAULT_DNS_SERVER` (`ip 127.0.0.1`,
`data-center DEFAULT_DATA_CENTER`, `auto-sync enable`). Die kent de **eigen** virtual servers al.

- ❌ **Niet doen**: de FortiADC zichzelf laten toevoegen als *FortiADC-SLB*-server op zijn eigen
  management-IP. Die `virtual-server-list` bleef leeg.
- ✅ **Wel doen**: `DEFAULT_DNS_SERVER` het juiste datacenter geven en daarnaar verwijzen in de pools.

### De andere FortiADC als FortiADC-SLB-server ✅

```
config global-load-balance servers
    edit "DEFAULT_DNS_SERVER"
        set data-center AMS
    next
    edit "FAD-FRA"
        set server-type FortiADC-SLB
        set ip 10.10.30.212          # management-IP van de andere FortiADC
        set port 5858
        set data-center FRA
        set auth-type auth_verify    # zie hieronder
        set auth-key <gedeeld-wachtwoord>
        set auto-sync enable
    next
end
```

Controle: `get global-load-balance servers FAD-FRA` toont onderaan de gesynchroniseerde virtual servers
(`== [ VS-SHOP-FRA ]` …). Die namen gebruik je als `server-member-name` in de GSLB-pools.

### Authenticatie van de sync 🟡

Twee plekken hebben een `auth-type` (`none`, `TCP_MD5SIG`, `auth_verify`) en een `auth-key`. De sleutel moet
op alle vier gelijk zijn:

| Waar | Betekenis |
|---|---|
| `config global-load-balance setting` → `auth-type` + `auth-key` | wat **deze** FortiADC eist van wie op poort 5858 verbindt ("Password of this device") |
| `config global-load-balance servers` → `edit FAD-<ander>` → `auth-type` + `auth-key` | waarmee deze FortiADC zich meldt bij de **andere** |

```
config global-load-balance setting       # op BEIDE FortiADC's, zelfde sleutel
    set auth-type auth_verify
    set auth-key <gedeelde-sleutel>
end
config global-load-balance servers
    edit "FAD-FRA"                         # op FAD-AMS; op FAD-FRA: "FAD-AMS"
        set auth-type auth_verify
        set auth-key <gedeelde-sleutel>
    next
end
```

Bevindingen op 8.0.4:

- ✅ Het veld in de setting heet **`auth-key`**, niet `password` zoals in de 7.4-documentatie (`set password`
  → *-284 CLI parsing error*).
- ✅ Het veld verschijnt pas na `set auth-type auth_verify` en is dan **verplicht** (in `set ?` staat er een
  `*` voor: `*auth-key  Password of this device.`). Zonder geeft `end` *-56 Empty value is not allowed* en
  wordt de hele setting niet opgeslagen: hij blijft op `auth-type none`.
- ✅ Met alleen de serverregel op `auth_verify` + `auth-key` en de setting op `none` synchroniseert het
  wél: de luisterende kant eist dan niets. Controleer dus altijd `get global-load-balance setting`, niet
  alleen of de sync werkt.
- 🟡 Nog te bevestigen: de sync met de sleutel aan beide kanten (lab: na *FortiADC-config opnieuw toepassen*).
- **`auth_verify`** heeft de voorkeur: de sleutel gaat versleuteld en het werkt ook met NAT ertussen.
  `TCP_MD5SIG` werkt niet door NAT heen.
- **Certificaten (optioneel, bovenop de sleutel):** `set user-defined-certificate enable` maakt `cert`
  verplicht (eigen certificaat van deze FortiADC). `set ca-verify enable` voegt `ca-group` en
  `intermediate-ca-group` toe (welke CA's de andere mag gebruiken). Zelfde velden op de serverregel. Niet
  in het lab gebruikt.
- Scherm poort 5858 daarnaast af, zodat alleen de FortiADC's die onderling bereiken.
- *Lab:* `dns.gslb_sync_auth` in `lab/topology.yaml` (standaard `auth_verify`). De controller leest de velden
  van de setting (`set ?` na het auth-type) en gebruikt het wachtwoordveld dat de versie kent; vindt hij er
  geen, dan probeert hij `auth-key`. (Automatiseren: de eerste regel van de contexthulp staat in de ruwe
  SSH-uitvoer achter de herhaalde invoer, alleen gescheiden door `\r`. Schoon de uitvoer eerst op, anders
  mis je juist `*auth-key`.) Na de
  GSLB-config controleert hij of elke FortiADC de virtual servers van de andere ziet (`verify_sync()`); zo
  niet, dan zet hij alles op `none` en meldt dat.
- Let op: een eerder gesynchroniseerde lijst kan blijven staan. Controleer na een wijziging de status, niet
  alleen of de namen er nog staan.

### Pools en hosts ✅

```
config global-load-balance virtual-server-pool
    edit "GP-SHOP-AMS"
        config member
            edit 1
                set server DEFAULT_DNS_SERVER      # op FAD-AMS; op FAD-FRA: FAD-AMS
                set server-member-name VS-SHOP-AMS
            next
        end
    next
    edit "GP-SHOP-FRA"
        config member
            edit 1
                set server FAD-FRA                 # op FAD-FRA: DEFAULT_DNS_SERVER
                set server-member-name VS-SHOP-FRA
            next
        end
    next
end
config global-load-balance host
    edit "GH-SHOP"
        set host-name shop
        set domain-name gslb.example.com.
        set load-balance-method global-availability
        config virtual-server-pool-list
            edit 1
                set virtual-server-pool GP-SHOP-AMS    # volgorde = voorkeur
            next
            edit 2
                set virtual-server-pool GP-SHOP-FRA
            next
        end
    next
end
```

De GSLB-config is daardoor per FortiADC **gelijkwaardig**, niet letterlijk gelijk: alleen wie
`DEFAULT_DNS_SERVER` is, verschilt.

*Lab:* `adcconfig.gslb(topo, local)`, `gslb_cleanup()`.

---

## 7. Global DNS: de vier valkuilen

Als de GSLB-objecten goed staan, kan de DNS nog steeds niets (goeds) antwoorden. Wij liepen achtereenvolgens
tegen deze vier aan; elk heeft een herkenbaar symptoom.

### 7.1 De DNS-server staat uit ✅

**Symptoom:** `dig @<port-ip> naam` geeft *connection refused* op poort 53.
**Oorzaak:** de global DNS-server staat in de fabrieksstand uit.

```
config global-dns-server general
    set gds-status enable
    set listen-on-all-interface enable
end
```

### 7.2 De standaardpolicy matcht eerst ✅

**Symptoom:** `NXDOMAIN`, zonder `aa`-vlag, met in de authority-sectie de SOA van de **root**
(`a.root-servers.net`), en `ra` staat aan. De FortiADC heeft de vraag recursief op internet opgezocht.
**Oorzaak:** policies matchen van boven naar beneden, en de ingebouwde `DEFAULT_DNS_POLICY`
(source/destination `any`, zonder zone, recursief) staat bovenaan.

```
config global-dns-server policy
    edit "DNS-LAB"
        set source-address any
        set destination-address any
        set zone-list fqdn_generate_gslb.example.com.
        set recursion-status disable
    next
    move "DNS-LAB" before "DEFAULT_DNS_POLICY"
end
```

Kanttekening: met `DEFAULT_DNS_POLICY` recursief is de FortiADC een **open resolver** voor iedereen die
poort 53 bereikt. Zet recursie daar uit als je hem niet nodig hebt.

### 7.3 De naam van de gegenereerde zone ✅

De GSLB-hosts genereren zelf een zone (type `fqdn-generate`, niet handmatig aan te maken). Op 8.0.4 heet die
**`fqdn_generate_<domein>.`**, bijvoorbeeld `fqdn_generate_gslb.adclab.internal.`. Die naam heb je nodig in
de `zone-list` van de policy. Opvragen: `show global-dns-server zone`.

### 7.4 TTL en nameserver van de gegenereerde zone 🟡

**Symptoom:** het antwoord heeft **TTL 86400**, en in de authority-sectie staat
`NS defaultprimary.<domein>` met `A 127.0.0.1`.
**Waarom dat telt:** met een TTL van een dag blijft een failover in caches hangen. Een resolver die het
NS-record van de FortiADC overneemt, gaat 127.0.0.1 (zichzelf) bevragen.

```
config global-dns-server zone
    edit "fqdn_generate_gslb.example.com."
        set ttl 30
        set negative-ttl 30
        set primary-server-name ns1        # zoals in de delegatie van de parent-zone
        set primary-server-ip 10.80.0.1    # het IP waarop deze FortiADC DNS beantwoordt
    next
end
```

🟡 Nog te bevestigen: of de FortiADC deze velden op een gegenereerde zone accepteert en behoudt.

### Delegatie in de parent-zone (best practice) ✅

De bestaande DNS blijft eigenaar van het domein en delegeert alleen een subdomein aan **beide** FortiADC's.
De vriendelijke naam is een CNAME:

```
gslb.example.com.      IN NS    ns1.gslb.example.com.
gslb.example.com.      IN NS    ns2.gslb.example.com.
ns1.gslb.example.com.  IN A     10.80.0.1     ; FortiADC datacenter A
ns2.gslb.example.com.  IN A     10.80.0.2     ; FortiADC datacenter B
shop.example.com.      IN CNAME shop.gslb.example.com.
```

Valt een heel datacenter weg (ook de FortiADC), dan vraagt de resolver het aan de andere nameserver.

*Lab:* `adcconfig.gslb()` (general), `dns_policy()`, `zone_settings()`; `deployer.bind_config()`.

---

## 8. GSLB en content routing (gedeelde VIP) ✅ (config) / 🟡 (failover)

**Probleem:** meerdere FQDN's (bijv. `portal` en `api`) delen één VIP, en content routing kiest de pool op
Host-header. De status van de virtual server, en dus ook die uit auto-sync, is "gezond" zolang **één** route
werkt. Valt alleen het portaal uit, dan blijft GSLB portal-gebruikers naar het kapotte datacenter sturen.

**Oplossing:** een *Generic Host*-server per datacenter, met per FQDN een eigen lid en een eigen health check
die de hele keten test (TLS, Host-header, content route, pool, applicatie):

```
config system health-check
    edit "HC-GSLB-PORTAL"
        set type https
        set port 443
        set method-type http_get
        set http-version http_1.1
        set hostname portal.example.com
        set send-string /healthz
        set receive-string "OK"
        set status-code 200
        set match-type match_all
    next
end
config global-load-balance servers
    edit "GEN-AMS"
        set server-type Generic-Host
        set data-center AMS
        config virtual-server-list
            edit "portal"
                set ip 10.80.0.120
                set health-check-inherit disable
                set health-check-ctrl enable
                set health-check-list HC-GSLB-PORTAL
            next
            edit "api"
                set ip 10.80.0.120
                set health-check-inherit disable
                set health-check-ctrl enable
                set health-check-list HC-GSLB-API
            next
        end
    next
end
```

---

## 9. Diagnose: DNS en GSLB stap voor stap

Werk van buiten naar binnen. Gebruik `dig` **zonder** `+short`: de status en de vlaggen vertellen het meeste.

```bash
dig @<lab-dns> desktop.example.com +short      # 1. werkt de gewone DNS?
dig @<lab-dns> shop.example.com                # 2. de hele keten (CNAME + GSLB)
dig @<fortiadc-dns-ip> shop.gslb.example.com   # 3. de FortiADC direct
```

| Wat je ziet (bij 3) | Oorzaak | Zie |
|---|---|---|
| *connection refused* | global DNS staat uit | 7.1 |
| time-out | port niet bereikbaar / routering | — |
| `REFUSED` | geen policy die matcht | 7.2 |
| `NXDOMAIN`, geen `aa`, SOA van de root | standaardpolicy matcht eerst (recursie) | 7.2 |
| `NXDOMAIN` met `aa` | zone wordt geserveerd, host bestaat niet (bijv. `domain-name` zonder punt) | 5 |
| `NOERROR`, `aa`, `ANSWER: 0` | host bestaat, geen gezond lid: pools, sync of health checks | 6, 8 |
| `NOERROR`, `aa`, A-record ✔ | werkt; let op TTL en NS-sectie | 7.4 |
| (bij 2) `SERVFAIL` terwijl 3 werkt | parent-DNS volgt de delegatie niet (glue, DNSSEC-validatie) | 7 |

Op de FortiADC:

```
show global-dns-server policy                    # volgorde en zone-list
show global-dns-server zone                      # naam van de gegenereerde zone
get global-load-balance servers <naam>           # status + gesynchroniseerde virtual servers
show global-load-balance servers                 # alle servers en hun virtual-server-list
get global-load-balance virtual-server-pool <naam>
```

Let op: `get global-load-balance servers` **zonder** naam toont alleen de namen, niet de virtual servers.

*Lab:* het demo-cockpit heeft **GSLB-controle: antwoord per DNS-server**. Die toont per GSLB-naam en per
server (FAD-AMS, FAD-FRA, lab-DNS) de status, de `aa`-vlag, de TTL en het IP. Zo zie je direct of de
TTL-instelling pakt en, tijdens een failover, wanneer het antwoord wisselt.

---

## 10. Demo-werkplek (Ubuntu desktop)

| Onderwerp | Bevinding / juiste aanpak | Status |
|---|---|---|
| Ubuntu cloud image + cloud-init (NoCloud-ISO `cidata`) | werkt op Proxmox, ook met `ubuntu-desktop-minimal` | ✅ |
| Firmware | Boot de cloud image met **UEFI** (`--machine q35 --bios ovmf --efidisk0 <storage>:1,efitype=4m,pre-enrolled-keys=0`). Met SeaBIOS bleef de desktop na de installatie (met `package_upgrade`) in een bootloop hangen; vermoedelijk een grub-update naar een verkeerd boot device. Zet er `--serial0 socket` bij: de kernel logt naar ttyS0, te lezen met `qm terminal`. | 🟡 |
| Firefox en eigen CA | gebruik de **deb uit Mozilla's APT-repo**; de snap leest geen enterprise policies/CA's uit `/etc`. Policy `Certificates.Install` + `ImportEnterpriseRoots`. | ✅ |
| Firefox en lab-DNS | zet **DNS-over-HTTPS uit** via policy, anders omzeilt Firefox de lab-DNS | ✅ |
| Split DNS | netplan: lab-NIC met `nameservers.search: [<labdomein>]` → systemd-resolved stuurt alleen dat domein naar de lab-DNS | ✅ |
| GSLB zichtbaar maken | DNS-cache uit in resolved (`Cache=no`) en Firefox (`network.dnsCacheExpiration=0`); lab-resolver `max-cache-ttl 5` | ✅ |
| Welkomstwizard | `gnome-initial-setup`, `update-notifier`, `apport` verwijderen + `~/.config/gnome-initial-setup-done` | 🟡 |
| RDP | xrdp: een **ingelogde console-sessie van dezelfde gebruiker geeft een zwart RDP-scherm**. Kies: autologin óf RDP. `.xsessionrc` met de Ubuntu-GNOME-variabelen, polkit-regel voor colord. | 🟡 |
| Private CA | **Name Constraints** (alleen het labdomein), zodat vertrouwen op een laptop geen risico is voor andere sites | ✅ |

---

## 11. Checklist voor een nieuwe uitrol

- [ ] Beide images (`boot.qcow2`, `data.qcow2`) als VirtIO-disks; 8 GB RAM; `--serial0 socket`
- [ ] Day-0 via de console (admin, leeg wachtwoord → direct nieuw wachtwoord)
- [ ] Licentie: serienummer controleren (`FADV0000000TRIAL` = geen licentie); daarna `execute vm-license`
- [ ] Tijdzone (`set zone`, check met `?`) en NTP
- [ ] WAF-profiel: `heuristic-sql-xss-injection-detect` (8.0)
- [ ] GSLB: eigen FortiADC = `DEFAULT_DNS_SERVER` (met datacenter); de andere als FortiADC-SLB, port 5858
- [ ] Sync-authenticatie: `auth-type auth_verify` + `auth-key` in `global-load-balance setting` én op de serverregel, op beide FortiADC's dezelfde sleutel; controleer met `get global-load-balance setting`
- [ ] Controle: `get global-load-balance servers <ander>` toont de virtual servers
- [ ] GSLB-hosts: `domain-name` met punt; `weight` of `global-availability`
- [ ] `config global-dns-server general` → `gds-status enable`
- [ ] Eigen DNS-policy **boven** `DEFAULT_DNS_POLICY`; recursie uit waar niet nodig
- [ ] Gegenereerde zone `fqdn_generate_<domein>.`: TTL en primaire nameserver
- [ ] Parent-zone: subdomein delegeren aan beide FortiADC's, CNAME's naar het subdomein
- [ ] Gedeelde VIP met content routing → Generic Host + health check per FQDN
- [ ] Test met `dig` zonder `+short` (zie de tabel in hoofdstuk 9)
