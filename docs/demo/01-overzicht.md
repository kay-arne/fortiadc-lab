# 1. Overzicht: het lab en het verhaal

**Kernboodschap:** FortiADC zit tussen gebruikers en applicaties en zorgt dat applicaties *beschikbaar*,
*snel* en *veilig* zijn, binnen één datacenter (SLB) en over datacenters heen (GSLB).

**Duur:** 5 minuten · **Nodig:** controller tab 2 (Lab) en tab 4 (Toegang)

## Het verhaal

Polder Retail verkoopt stroopwafels en klompen via een webshop. De shop draait in twee datacenters:
**Amsterdam** (primair) en **Frankfurt** (uitwijk). Medewerkers gebruiken een **portaal**, partners een
**orders-API**. Daarnaast zijn er twee oudere webapplicaties (*Juice Shop* en *DVWA*) met bekende
kwetsbaarheden die niet op korte termijn gepatcht kunnen worden.

## Architectuur

```
  presentator ── vmbr0 ──┬── port1 FAD-AMS ── port3 ── Datacenter Amsterdam (10.81.0.0/24)
   (management,          │        │
    GSLB-sync)           └── port1 FAD-FRA ── port3 ── Datacenter Frankfurt (10.82.0.0/24)
                                  │  │
                          port2 ──┘  └── port2
                                  │
                  "Internet" (10.80.0.0/24): desktop .10, client .20, DNS .53
                  VIP's AMS 10.80.0.1xx (FAD-AMS), FRA 10.80.0.2xx (FAD-FRA)
                  GSLB-DNS: ns1 = 10.80.0.1 (FAD-AMS), ns2 = 10.80.0.2 (FAD-FRA)
```

| Applicatie | Naam | Wat laat het zien |
|---|---|---|
| Polder Webshop | `shop.adclab.internal` | SLB, health checks, persistence, GSLB actief/standby |
| Medewerkersportaal | `portal.adclab.internal` | content routing (host + pad), GSLB per FQDN |
| Orders API | `api.adclab.internal` | content routing, GSLB per FQDN |
| OWASP Juice Shop | `juice.adclab.internal` | WAF blokkeert |
| DVWA | `dvwa.adclab.internal` | WAF eerst monitoren, dan blokkeren (in Frankfurt) |

## Demo

| Zeg | Doe / laat zien |
|---|---|
| "Dit is een volledig virtueel lab op Proxmox. Twee datacenters, elk met een eigen FortiADC, een DNS-server en een desktop." | Controller → **2. Lab**: de segmenten en containers. |
| "Elke webserver is een kleine container. Ze vertellen zelf wie ze zijn, zodat we load balancing letterlijk zien." | Desktop → *Polder Webshop*. Wijs op *Geserveerd door shop-ams-1 (AMS)*. |
| "De desktop vertrouwt onze eigen lab-CA. Dat certificaat is beperkt tot dit labdomein, een best practice voor private CA's." | Klik op het slotje → certificaat → uitgever *FortiADC Lab Root CA*. |
| "De namen komen uit een gewone DNS-server. Het subdomein `gslb` is gedelegeerd aan beide FortiADC's: zij beslissen samen per vraag naar welk datacenter je gaat." | Controller → **4. Toegang** → tabel *Namen in het lab*. |
| "Alles wat je ziet is gegenereerd uit één YAML-bestand en als gewone CLI op de FortiADC's gezet." | **2. Lab → Config bekijken**. |

**Overgang:** "Laten we beginnen bij de basis: hoe verdeelt de FortiADC verkeer over servers?"
