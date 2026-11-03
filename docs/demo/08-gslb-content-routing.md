# 8. GSLB in combinatie met content routing

**Kernboodschap:** als meerdere applicaties één VIP delen (content routing), is "de VIP leeft" niet genoeg om
te bepalen of een applicatie beschikbaar is. Met een **Generic Host** en een **health check per FQDN** (met
de juiste Host-header) faalt elke applicatie onafhankelijk over naar het andere datacenter.

**Duur:** 10 minuten · **Objecten:** `GEN-AMS` / `GEN-FRA` (Generic Host) met in de virtual-server-lijst
`portal` en `api` op hetzelfde IP, elk met eigen health check `HC-GSLB-PORTAL` / `HC-GSLB-API`;
hosts `GH-PORTAL` en `GH-API` met methode *none* (gewogen, actief/actief). Op beide FortiADC's gelijk.

## Het probleem

```
                    VIP 10.80.0.120 (AMS)              VIP 10.80.0.220 (FRA)
portal.adclab…  ─►  content route → POOL-PORTAL-AMS    content route → POOL-PORTAL-FRA
api.adclab…     ─►  content route → POOL-API-AMS       content route → POOL-API-FRA
```

Valt alleen `portal-ams-1` uit, dan antwoordt de VIP in Amsterdam nog steeds (voor de API). Een health check
op "de VIP" ziet niets, en GSLB blijft gebruikers van het portaal naar een kapot portaal sturen. Ook de
auto-sync van een *FortiADC-SLB*-server helpt hier niet: die kent de status van de virtual server
`VS-APPS-AMS`, en die is gezond zolang één van zijn content routes werkt.

## De oplossing in dit lab

| GSLB-server | Lid in de virtual-server-lijst | IP | Health check |
|---|---|---|---|
| `GEN-AMS` | `portal` | 10.80.0.120 | `HC-GSLB-PORTAL`: HTTPS, Host `portal.adclab.internal`, `GET /healthz` → 200 + `OK` |
| `GEN-AMS` | `api` | 10.80.0.120 | `HC-GSLB-API`: HTTPS, Host `api.adclab.internal`, `GET /api/healthz` → 200 + `OK` |
| `GEN-FRA` | `portal` / `api` | 10.80.0.220 | idem |

De health check gaat door de hele keten: TLS, de content route, de pool en de applicatie. Precies wat een
gebruiker doet. Beide FortiADC's voeren hem uit naar beide datacenters, dus ze komen tot hetzelfde antwoord.

## Demo

| Zeg | Doe / laat zien |
|---|---|
| "Portal en API zijn actief/actief: beide datacenters krijgen verkeer." | Cockpit → **Volg portal 60 s**: antwoorden wisselen tussen `10.80.0.120` en `10.80.0.220`. |
| "In de FortiADC: per datacenter één Generic Host, maar twee leden op hetzelfde IP, elk met een eigen health check." | GUI (FAD-AMS of FAD-FRA): server `GEN-AMS` → virtual server list → `portal` en `api`. Open `HC-GSLB-PORTAL`: HTTP 1.1, hostname `portal.adclab.internal`. |
| "Nu gaat alleen het portaal in Amsterdam stuk. De API in Amsterdam blijft gezond." | Cockpit → **Alleen portal in AMS laten falen**. |
| "De lokale load balancer haalt de server uit de pool. De GSLB-check voor `portal` in AMS faalt, die voor `api` niet." | GUI: `POOL-PORTAL-AMS` down; GSLB-status: `GEN-AMS/portal` down, `GEN-AMS/api` up. |
| "Resultaat: portal-gebruikers gaan allemaal naar Frankfurt, API-gebruikers blijven over beide verdeeld." | Cockpit → **Volg portal 60 s**: alleen nog `10.80.0.220`. Cockpit → **DNS: wat antwoordt…**: `api` nog op beide. |
| "Zonder health check per FQDN had de helft van de portal-gebruikers nu een foutmelding." | Benoem de les: *health check wat de gebruiker gebruikt, niet wat het netwerk ziet*. |
| "Herstel." | Cockpit → **portal in AMS herstellen**. |

## Best practices

- Health check de **applicatie** (pad + verwachte inhoud), niet alleen de poort.
- Bij gedeelde VIP's: **één GSLB-lid en één health check per FQDN**, met de juiste Host-header (en SNI).
- Gebruik een apart, licht health-endpoint (`/healthz`) dat de echte afhankelijkheden controleert.
- Kies de TTL bewust: kort genoeg voor snelle failover, lang genoeg om de DNS niet te overbelasten.
