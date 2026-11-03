# 7. GSLB: twee datacenters, twee FortiADC's

**Kernboodschap:** load balancing binnen een datacenter beschermt tegen het uitvallen van een server. GSLB
beschermt tegen het uitvallen van een héél datacenter. Elk datacenter heeft een eigen FortiADC; samen
beantwoorden ze de DNS en weten ze van elkaar welke applicaties gezond zijn. Valt een datacenter inclusief
zijn FortiADC weg, dan neemt het andere het volledig over.

**Duur:** 15 minuten · **Objecten:** DNS-zone `adclab.internal` (BIND), delegatie `gslb.adclab.internal` →
beide FortiADC's, GSLB-servers `DEFAULT_DNS_SERVER` (de FortiADC zelf) en `FAD-<ander datacenter>`
(type *FortiADC-SLB*, auto-sync), host `GH-SHOP`
(global availability: eerst `GP-SHOP-AMS`, dan `GP-SHOP-FRA`)

## Hoe het in elkaar zit

```
desktop ──► lab-DNS (BIND, 10.80.0.53)
              shop.adclab.internal   CNAME  shop.gslb.adclab.internal
              gslb.adclab.internal   NS     ns1.gslb (10.80.0.1 = FAD-AMS)
                                     NS     ns2.gslb (10.80.0.2 = FAD-FRA)
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
     FAD-AMS (Amsterdam)  ◄──────►  FAD-FRA (Frankfurt)       GSLB-sync over management (port1)
     VS-SHOP-AMS 10.80.0.111        VS-SHOP-FRA 10.80.0.211   auto-sync: elk kent de VS'en en
     POOL-SHOP-AMS                  POOL-SHOP-FRA             hun status van de ander
```

- **Beide FortiADC's zijn authoritative** voor `gslb.adclab.internal` en hebben dezelfde GSLB-configuratie
  (data centers, servers, pools, hosts). Het maakt niet uit wie de vraag krijgt: het antwoord is gelijk.
- **Auto-sync**: elke FortiADC kent zijn eigen virtual servers via de ingebouwde GSLB-server
  `DEFAULT_DNS_SERVER`, en de andere FortiADC als GSLB-server van het type *FortiADC-SLB* (`FAD-FRA` op
  FAD-AMS, `FAD-AMS` op FAD-FRA). Van die server haalt hij de virtual servers en hun status op. Heeft
  `VS-SHOP-AMS` geen gezonde poolleden meer, dan weet FAD-FRA dat ook.
- **Delegatie van een subdomein** (best practice): de bestaande DNS blijft eigenaar van het domein; alleen
  `gslb.` gaat naar de FortiADC's, en de vriendelijke naam is een CNAME daarnaartoe.

## Voorbereiding

- Cockpit → **Alles herstellen** (zet ook een uitgeschakelde FortiADC weer aan).
- GUI van **beide** FortiADC's open (tab 4 van de controller heeft de links), elk op *Global Load Balance*.
- Desktop: een terminal open (voor `dig`).

## Demo

### Stap 1: twee FortiADC's, één GSLB

| Zeg | Doe / laat zien |
|---|---|
| "De gewone DNS kent `shop` alleen als verwijzing naar het GSLB-subdomein." | Desktop: `dig shop.adclab.internal` → CNAME `shop.gslb…` en A `10.80.0.111`. |
| "Dat subdomein is gedelegeerd aan twee nameservers: de FortiADC in Amsterdam en die in Frankfurt." | `dig NS gslb.adclab.internal` → `ns1` en `ns2`. |
| "Vraag het aan elk van beide: hetzelfde antwoord." | `dig +short @10.80.0.1 shop.gslb.adclab.internal` en `@10.80.0.2`: beide `10.80.0.111`. |
| "Ze kennen elkaar: in Amsterdam staat Frankfurt als GSLB-server, en omgekeerd. De virtual servers komen er vanzelf in via auto-sync." | GUI AMS: *Global Load Balance → Servers*: `DEFAULT_DNS_SERVER` (de eigen VS'en) en `FAD-FRA` (die van Frankfurt), elk met hun virtual server list. Cockpit → **GSLB-status op beide FortiADC's**. |
| "De webshop is actief/standby: Amsterdam primair, Frankfurt uitwijk." | GUI: host `GH-SHOP`, methode *global availability*, pool-lijst AMS → FRA. |

### Stap 2: alle webservers in Amsterdam vallen uit

| Zeg | Doe / laat zien |
|---|---|
| "We kijken live mee." | Cockpit → **Volg shop 60 s**, of desktop: `watch -n1 dig +short shop.adclab.internal`. |
| "Een slechte release: alle webservers in Amsterdam liggen eruit. De FortiADC in Amsterdam draait gewoon door." | Cockpit → **Amsterdam: alle webservers UIT**. |
| "FAD-AMS ziet de pool leeg raken, de virtual server gaat down, en via auto-sync weet FAD-FRA dat ook." | GUI AMS: `POOL-SHOP-AMS` rood. GUI FRA: server `FAD-AMS` → `VS-SHOP-AMS` down. |
| "Beide nameservers antwoorden nu Frankfurt." | Log: `10.80.0.111` → `10.80.0.211`. Desktop: privévenster → *Geserveerd door shop-fra-… (FRA)*. |
| "Herstel." | Cockpit → **Amsterdam: webservers AAN** → antwoord terug naar `10.80.0.111`. |

### Stap 3: het hele datacenter valt weg, inclusief de FortiADC

| Zeg | Doe / laat zien |
|---|---|
| "Nu het echte scenario: stroomuitval in Amsterdam. Servers én load balancer weg." | Cockpit → **Amsterdam: complete uitval (ook FortiADC)**. Proxmox: `adclab-fad-ams` gaat uit. |
| "Daarom staan er twee nameservers in de delegatie: `ns1` antwoordt niet meer, de resolver vraagt het aan `ns2` in Frankfurt." | Desktop: `dig @10.80.0.1 …` → time-out; `dig shop.adclab.internal` → `10.80.0.211`. |
| "FAD-FRA kan FAD-AMS niet meer bereiken en sluit zijn virtual servers uit van de antwoorden." | GUI FRA: server `FAD-AMS` onbereikbaar. |
| "Voor de gebruiker: de webshop werkt gewoon, vanuit Frankfurt." | Desktop: webshop (privévenster). |
| "Herstel: de FortiADC start op, synchroniseert, en Amsterdam wordt weer primair." | Cockpit → **Amsterdam: herstellen (FortiADC + servers)** (de FortiADC heeft een paar minuten nodig). |

## Achtergrond voor vragen

- **TTL en caching**: failover-tijd = detectie (health check + sync) + TTL + caching in resolvers en
  browsers. De FortiADC's geven GSLB-antwoorden een TTL van 30 s (`dig` toont hem); de lab-DNS cachet
  maximaal 5 s en Firefox niet (policy), zodat je het live ziet. In productie kies je de TTL bewust: korter
  betekent snellere failover maar meer DNS-verkeer.
- **Methoden**: *global availability* (actief/standby), *weight* (gewogen, actief/actief, script 8) en
  *topology* (op basis van de locatie van de client).
- **Sync-beveiliging**: in productie authenticeer je de GSLB-sync (TCP-MD5 of auth-verify). In het lab loopt
  hij onversleuteld over het managementnetwerk.
- **Generic Host** gebruik je voor servers of load balancers die geen FortiADC zijn, én als meerdere FQDN's
  één VIP delen: zie script 8.

## Terugzetten

Cockpit → **Alles herstellen**.
