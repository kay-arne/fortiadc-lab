# 2. Load balancing & persistence

**Kernboodschap:** een virtual server (VIP) verdeelt verbindingen over een pool van realservers. De
methode is per applicatie te kiezen, en *persistence* zorgt dat een gebruiker met een winkelmandje bij
dezelfde server blijft.

**Duur:** 10 minuten · **Objecten:** `VS-SHOP-AMS`, `POOL-SHOP-AMS`, `PERS-COOKIE`, `LB_METHOD_ROUND_ROBIN`

## Voorbereiding

- Cockpit → **Alles herstellen**.
- GUI van **FAD-AMS** open op *Server Load Balance → Virtual Server* (deze demo speelt in Amsterdam).

## Demo

### Stap 1: de virtual server

| Zeg | Doe / laat zien |
|---|---|
| "De webshop heeft één adres voor de buitenwereld: de VIP `10.80.0.111` op port2." | GUI: open `VS-SHOP-AMS`. Wijs op *Address*, *Port 443*, *Profile LB_PROF_HTTPS*. |
| "Achter die VIP zit een pool van twee servers in Amsterdam." | Tab *Real Server Pool* → `POOL-SHOP-AMS`: `RS-shop-ams-1` en `-2`. |
| "Het is een full proxy: de FortiADC termineert de verbinding van de client en maakt zelf een nieuwe naar de server." | Desktop: webshop, tabel onderaan: *Verbinding van* is het interface-IP van de FortiADC (`10.81.0.1`), niet de client. |

### Stap 2: verdeling zonder persistence

| Zeg | Doe / laat zien |
|---|---|
| "We sturen twintig verzoeken zonder cookies, zoals twintig verschillende bezoekers." | Cockpit → **20× shop zonder cookie**. |
| "Round robin: netjes om en om." | Log: verdeling ~10/10 over `shop-ams-1` en `shop-ams-2`. |

> Het verzoek naar `/whoami` valt níet onder de persistence-cookie, omdat de client-tool geen cookies bewaart.
> Dat is precies het verschil met de volgende stap.

### Stap 3: persistence met een cookie

| Zeg | Doe / laat zien |
|---|---|
| "Maar een winkelmandje leeft vaak in het geheugen van één server. Dan wil je dat een bezoeker blijft plakken." | GUI: `VS-SHOP-AMS` → *Persistence* `PERS-COOKIE` (type *insert cookie*, naam `FADCLAB`). |
| "De FortiADC zet zelf een cookie met de naam van de server. De applicatie hoeft niets te weten." | Cockpit → **20× shop mét cookie**: alle 20 naar dezelfde server. |
| "In de browser zie je hetzelfde." | Desktop: webshop, **Auto-verversen (1 s)** aan: de server blijft gelijk. Tabel: *Persistence-cookie* `FADCLAB=shop-ams-…`. Open een privévenster: kans op de andere server. |

### Stap 4 (optioneel): methode wisselen

| Zeg | Doe / laat zien |
|---|---|
| "Round robin is simpel; *least connection* of *fastest response* past beter als servers ongelijk belast zijn." | GUI: `VS-SHOP-AMS` → *Method* → `LB_METHOD_LEAST_CONNECTION` → opslaan. |
| "We maken één server traag." | Cockpit → realserver `shop-ams-2` → **Traag** (2 s per response). |
| "Bij least connection lopen de verbindingen naar de trage server op, dus krijgt de snelle meer." | Cockpit → **20× shop zonder cookie**: verdeling schuift naar `shop-ams-1`. |

## Terugzetten

- Cockpit → `shop-ams-2` → **Snel**; methode in de GUI terug naar `LB_METHOD_ROUND_ROBIN`
  (of **2. Lab → FortiADC-config opnieuw toepassen**).

**Overgang:** "Wat gebeurt er als een server niet alleen traag is, maar echt stuk gaat?"
