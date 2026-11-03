# 4. SSL offloading en certificaten

**Kernboodschap:** de FortiADC termineert TLS centraal. Certificaten en TLS-beleid beheer je op één plek; de
servers hoeven geen certificaten te hebben en worden ontlast. Daarna kan de FortiADC in het verkeer kijken
(content routing, WAF).

**Duur:** 5 minuten · **Objecten:** `LAB-SHOP` (certificaat), `LCG-SHOP` (groep), `CSSL-SHOP` (client SSL
profile, TLS 1.2 en 1.3)

## Demo

| Zeg | Doe / laat zien |
|---|---|
| "De gebruiker ziet een geldig certificaat voor `shop.adclab.internal`." | Desktop: slotje → certificaat: SAN's `shop.adclab.internal` en `shop.gslb.adclab.internal`, uitgever *FortiADC Lab Root CA*. |
| "Dat certificaat staat op de FortiADC's, niet op de webservers: die praten gewoon HTTP op poort 80 in het datacenter. Beide datacenters hebben hetzelfde certificaat, anders breekt een GSLB-failover." | GUI FAD-AMS én FAD-FRA: *System → Certificates → Local*: `LAB-SHOP`. Daarna `VS-SHOP-AMS` → *Client SSL Profile* `CSSL-SHOP`. |
| "In het client SSL profile leg je het TLS-beleid vast: hier alleen TLS 1.2 en 1.3." | GUI: `CSSL-SHOP` → toegestane versies. |
| "Even vanaf de commandline bekeken." | Cockpit → **Certificaat & TLS van shop**: TLS-versie, cipher, uitgever en *vertrouwd: ja*. |
| "Eén VIP kan meerdere namen bedienen: het certificaat van het portaal heeft vier SAN's." | Cockpit → **Certificaat & TLS van portal**. |
| "Wie per ongeluk `http://` intypt, wordt netjes doorgestuurd." | Desktop: typ `http://shop.adclab.internal` → komt op `https://` uit (*HTTP to HTTPS redirect* op de VS). |

## Best practices in dit lab (goed om te benoemen)

- **Private CA met Name Constraints**: de lab-root-CA mag alleen certificaten uitgeven onder
  `adclab.internal`. Vertrouwt een laptop deze CA, dan loopt die geen risico voor andere sites.
- **Korte looptijd**: servercertificaten zijn 397 dagen geldig (browsers accepteren maximaal 398).
- **SAN's, één sleutel per certificaat**, `serverAuth` als EKU.
- **Geen oude protocollen**: TLS 1.0/1.1 uit.

## Uitbreiding (vragen uit de zaal)

- *Re-encryptie naar de server*: met een *real server SSL profile* op de pool versleutelt de FortiADC ook
  naar de backend (end-to-end TLS) en kan hij toch inspecteren.
- *ACME/Let's Encrypt*: FortiADC kan certificaten automatisch aanvragen en verlengen (type *Automated*).
