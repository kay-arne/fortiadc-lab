# 0. Voorbereiding en spelregels

Deze map bevat de demo-scripts van het FortiADC Lab. Elk script is los te geven, maar samen vormen ze één
verhaal: **Polder Retail**, een webwinkel met twee datacenters (Amsterdam en Frankfurt), elk met een eigen
FortiADC, een
medewerkersportaal, een orders-API en een paar oudere webapplicaties die beveiligd moeten worden.

| # | Script | Use case | Duur |
|---|---|---|---|
| 1 | [Overzicht](01-overzicht.md) | het lab en het verhaal | 5 min |
| 2 | [Load balancing & persistence](02-load-balancing.md) | virtual server, pool, LB-methodes, cookie-persistence | 10 min |
| 3 | [Health checks](03-health-checks.md) | server valt uit, komt terug, wordt traag | 10 min |
| 4 | [SSL offloading](04-ssl-offloading.md) | certificaten, TLS 1.2+, HTTP → HTTPS | 5 min |
| 5 | [Content routing](05-content-routing.md) | één VIP, routeren op host en pad | 10 min |
| 6 | [Web Application Firewall](06-waf.md) | signatures, SQLi/XSS, monitoren vs. blokkeren | 15 min |
| 7 | [GSLB: twee FortiADC's](07-gslb.md) | DNS-delegatie, auto-sync, failover incl. uitval van een FortiADC | 15 min |
| 8 | [GSLB + content routing](08-gslb-content-routing.md) | generic host met health check per FQDN | 10 min |
| 9 | [Zichtbaarheid](09-zichtbaarheid.md) | FortiView, traffic- en security-logs | 5 min |

## Schermindeling (aanbevolen)

- **Links**: de Ubuntu desktop (Proxmox-console of RDP) met Firefox. De bladwijzerbalk *FortiADC Lab* bevat
  alle applicaties. Het certificaat is vertrouwd: geen waarschuwingen.
- **Rechts**: de GUI van **FAD-AMS** en **FAD-FRA** (elk een tabblad) en het **demo-cockpit** van de controller (tab 3).

Het cockpit heeft per use case knoppen die verkeer genereren, storingen veroorzaken of aanvallen uitvoeren.
Alles wat het cockpit doet, kun je ook met de hand op de desktop doen; het cockpit is er om het
**voorspelbaar en snel** te maken tijdens een presentatie.

## Checklist vóór elke demo (5 minuten)

1. Controller → **2. Lab**: alles groen, *FortiADC-configuratie: toegepast op beide* (zonder waarschuwingen).
2. Controller → **3. Demo-cockpit** → **Alles herstellen**. Dit start een uitgeschakelde FortiADC, zet alle
   realservers op *health ok* zonder vertraging, en zet de WAF van DVWA terug op *monitoren*.
3. Desktop: open Firefox en klik op *Polder Webshop*. Je hoort de webshop te zien met
   *Geserveerd door shop-ams-…* en een slotje zonder waarschuwing.
4. GUI van beide FortiADC's: ingelogd (sessie blijft 8 uur open), dashboard open.
5. Cockpit → GSLB → **DNS: wat antwoordt de lab-DNS nu?** Verwacht: `shop` → `10.80.0.111` (Amsterdam).

> Menunamen in de FortiADC-GUI verschillen per versie iets. De scripts noemen de namen van FortiADC 7.4/7.6;
> zoek bij twijfel op het objectnaam-prefix (`VS-`, `POOL-`, `HC-`, `GH-`).

## Naamgeving in de configuratie

Alles is gegenereerd uit `lab/topology.yaml` en volgt één schema, zodat je in de GUI snel vindt wat je zoekt.
SLB-objecten staan op de FortiADC van hun datacenter (`…-AMS` op FAD-AMS, `…-FRA` op FAD-FRA); de
GSLB-objecten staan op beide, identiek:

| Prefix | Object | Voorbeeld |
|---|---|---|
| `VS-` | virtual server per datacenter | `VS-SHOP-AMS` |
| `POOL-` | real server pool | `POOL-SHOP-AMS` |
| `RS-` | real server | `RS-shop-ams-1` |
| `HC-` | health check naar een realserver | `HC-SHOP` |
| `HC-GSLB-` | health check van GSLB naar een VIP, per FQDN | `HC-GSLB-PORTAL` |
| `CR-` | content route | `CR-APPS-AMS-API` |
| `CSSL-`, `LCG-`, `LAB-` | client SSL profile, certificaatgroep, certificaat | `CSSL-SHOP` |
| `WAF-LAB-` | WAF-profiel | `WAF-LAB-BLOCK` |
| `FAD-` | GSLB-server type FortiADC-SLB (een FortiADC, auto-sync) | `FAD-FRA` |
| `GEN-` | GSLB-server type Generic Host (per datacenter, voor gedeelde VIP's) | `GEN-AMS` |
| `GP-`, `GH-` | GSLB-pool, GSLB-host | `GH-SHOP` |

De volledige config zie je in de controller onder **2. Lab → Config bekijken**: handig om tijdens een demo te
laten zien dat het "gewoon CLI" is.

## Als iets niet werkt

| Symptoom | Oorzaak / oplossing |
|---|---|
| Certificaatwaarschuwing op de desktop | Je gebruikt een IP of `vip-…`-naam. Gebruik de namen uit de bladwijzers. |
| `shop` blijft op Amsterdam na een failover | Browser hergebruikt de open verbinding. Open een privévenster of wacht ~10 s. |
| Juice Shop laadt niet volledig met `WAF-LAB-BLOCK` | False positive op een legitiem verzoek: mooi bruggetje naar *exceptions* (zie script 6). |
| Cockpit-knop geeft `HTTP 503` | Alle servers van die pool zijn down: precies wat de health check hoort te doen. **Alles herstellen**. |
| Waarschuwing *regels geweigerd* op de Lab-tab | Een optie heet anders in jouw FortiADC-build. Klik **Toon**, de rest van de config staat. |
