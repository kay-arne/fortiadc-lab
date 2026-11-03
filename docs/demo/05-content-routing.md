# 5. Content routing

**Kernboodschap:** omdat de FortiADC het HTTP-verkeer ziet (na SSL offloading), kan hij per verzoek kiezen
naar welke pool het gaat: op hostnaam, pad, header of bron-IP. Eén VIP en één certificaat voor meerdere
applicaties.

**Duur:** 10 minuten · **Objecten:** `VS-APPS-AMS` met de routes `CR-APPS-AMS-API-PATH`,
`CR-APPS-AMS-API` en `CR-APPS-AMS-PORTAL`

## De regels (in volgorde)

| Volgorde | Content route | Voorwaarde | Pool |
|---|---|---|---|
| 1 | `CR-APPS-AMS-API-PATH` | URL begint met `/api/` | `POOL-API-AMS` |
| 2 | `CR-APPS-AMS-API` | Host = `api.adclab.internal` | `POOL-API-AMS` |
| 3 | `CR-APPS-AMS-PORTAL` | Host = `portal.adclab.internal` | `POOL-PORTAL-AMS` |

De eerste regel die past, wint. Daarom staat het specifieke pad bovenaan.

## Demo

| Zeg | Doe / laat zien |
|---|---|
| "Portal en API delen één VIP: `10.80.0.120`. Kijk maar in DNS." | Cockpit → **DNS: wat antwoordt de lab-DNS nu?**: `portal` en `api` → beide `10.80.0.120` (via GSLB). |
| "Op basis van de hostnaam gaat het portaal naar de portal-servers…" | Cockpit → **portal → portal-pool**: alles `portal-ams-1`. Desktop: *Medewerkersportaal* (blauw). |
| "…en de API naar de API-servers." | Cockpit → **api → API-pool (host)**: `api-ams-1`. Desktop: *Orders API* (JSON). |
| "Maar ook op pad: alles onder `/api/` van het portaal komt bij de API terecht. Typisch voor een frontend en een backend op dezelfde site." | Cockpit → **portal/api/… → API-pool (pad)**: `api-ams-1`. Desktop: bladwijzer *Portal → /api/orders*: JSON, maar de adresbalk zegt `portal`. |
| "Zo staat het in de FortiADC." | GUI: `VS-APPS-AMS` → *Content Routing* aan, lijst van drie routes. Open `CR-APPS-AMS-API-PATH`: *HTTP Request URL*, regex `^/api/`. |
| "Wat niet matcht, komt er niet in: dat is ook een vorm van beveiliging." | Desktop: `https://vip-apps-ams.adclab.internal/` → certificaatwaarschuwing én geen route. (Optioneel; leg uit waarom.) |

## Achtergrond voor vragen

- Andere voorwaarden: *HTTP Referer*, *SNI*, *bron-IP*, en combinaties (AND).
- Elke route kan een eigen persistence en LB-methode hebben, of die van de VS overerven.
- In script 8 zie je waarom deze opzet extra aandacht vraagt bij GSLB: één VIP, maar meerdere applicaties
  die los van elkaar kunnen uitvallen.
