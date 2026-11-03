# 3. Health checks

**Kernboodschap:** de FortiADC controleert continu of elke server écht werkt, niet alleen of hij pingt. Een
server die fouten geeft, gaat uit de pool, zonder dat een gebruiker het merkt, en komt vanzelf terug.

**Duur:** 10 minuten · **Objecten:** `HC-SHOP` (HTTP `GET /healthz`, verwacht status 200 én tekst `OK`)

## Voorbereiding

- Cockpit → **Alles herstellen**, daarna onder *Realservers* → **Toestand ophalen**.
- GUI van **FAD-AMS**: *Server Load Balance → Real Server Pool → POOL-SHOP-AMS* (ledenstatus) of het dashboard met de
  status van de realservers.

## Demo

### Stap 1: wat controleert de health check?

| Zeg | Doe / laat zien |
|---|---|
| "Een ping zegt alleen dat de server aanstaat. Wij vragen een echte pagina op en controleren statuscode én inhoud." | GUI: *Shared Resources → Health Check → HC-SHOP*: type HTTP, `/healthz`, *match all* op `200` en `OK`. |
| "Elke 5 seconden, met een timeout van 3." | Interval/timeout in hetzelfde scherm. |

### Stap 2: applicatiefout (server draait, maar is ziek)

| Zeg | Doe / laat zien |
|---|---|
| "Stel: een deployment gaat mis. De server draait, maar de applicatie geeft fouten." | Cockpit → `shop-ams-1` → **Fail**. |
| "Binnen een paar seconden ziet de FortiADC dat en haalt hem uit de pool." | GUI: `RS-shop-ams-1` wordt rood/down. |
| "Gebruikers merken niets: alles gaat naar de gezonde server." | Desktop: webshop met auto-verversen: alleen nog `shop-ams-2`. Cockpit → **20× shop zonder cookie**: 20/0. |
| "Ook persistence respecteert dat: een bezoeker die aan de zieke server plakte, wordt netjes omgezet." | Desktop: het cookie wijst naar de nieuwe server. |

### Stap 3: herstel

| Zeg | Doe / laat zien |
|---|---|
| "Fout opgelost: de server komt automatisch terug, er hoeft niemand iets te doen." | Cockpit → `shop-ams-1` → **OK**. GUI: groen binnen ~5-10 s. |

### Stap 4: harde crash en alle servers weg

| Zeg | Doe / laat zien |
|---|---|
| "Nu crasht het proces echt." | Cockpit → `shop-ams-1` → **Stop**: zelfde effect, nu *connection refused*. |
| "En als de hele pool weg is?" | Cockpit → `shop-ams-2` → **Stop**. Desktop: foutpagina (503) van de FortiADC. |
| "Dat is het moment voor GSLB: dan moet het andere datacenter het overnemen. Daar komen we op terug." | Laat de 503 zien via `https://vip-shop-ams.adclab.internal` (certificaatwaarschuwing negeren) of de cockpit-log; dan **Alles herstellen**. (Via `shop.adclab.internal` zie je hem niet: de GSLB stuurt je dan al naar Frankfurt.) |

## Achtergrond voor vragen

- Per pool kun je meerdere health checks combineren (AND/OR); per poollid kun je ze overschrijven.
- Het lab gebruikt aparte health checks voor GSLB (`HC-GSLB-…`): die testen de VIP via HTTPS mét de juiste
  Host-header, dus de hele keten. Zie script 8.

## Terugzetten

Cockpit → **Alles herstellen**.
