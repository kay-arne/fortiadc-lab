# 6. Web Application Firewall

**Kernboodschap:** kwetsbare applicaties kun je niet altijd snel patchen. De FortiADC-WAF beschermt ze
direct: met signatures (FortiGuard) en heuristische SQL-injectie/XSS-detectie. Begin in *monitor*-modus om
te leren, schakel daarna over naar *blokkeren*.

**Duur:** 15 minuten · **Objecten:** `WAF-LAB-BLOCK` (op `VS-JUICE-AMS`, FAD-AMS), `WAF-LAB-MONITOR` (op
`VS-DVWA-FRA`, FAD-FRA)

## Voorbereiding

- Cockpit → **Alles herstellen** (DVWA staat dan op *monitoren*).
- DVWA draait in Frankfurt, Juice Shop in Amsterdam: open bij elke stap de GUI van de FortiADC van dat
  datacenter, op *Web Application Firewall → WAF Profile* en *Log & Report → Security Log → WAF*.
- Desktop: DVWA-login is `admin` / `password` (standaard van DVWA), beveiligingsniveau *low*.

> De aanvalsset gebruikt onschuldige teststrings die op echte technieken lijken (SQL-injectie, XSS, path
> traversal, command injection, Log4Shell, scanner-user-agent) en raakt alleen de eigen lab-apps.

## Demo

### Stap 1: monitoren (DVWA)

| Zeg | Doe / laat zien |
|---|---|
| "DVWA is bewust lek. We zetten de WAF eerst alleen op monitoren: zien wat er gebeurt, niets breken." | GUI FAD-FRA: `VS-DVWA-FRA` → WAF-profiel `WAF-LAB-MONITOR` (*Alert-Only*). |
| "Een SQL-injectie, met de hand." | Desktop: DVWA → *SQL Injection* → User ID: `1' OR '1'='1` → alle gebruikers verschijnen. |
| "De aanval slaagt, maar de FortiADC heeft hem gezien." | GUI: Security Log → WAF: melding met de aanvalstype en *action: alert*. |
| "Nu de volledige testset." | Cockpit → **Aanvalsset → DVWA**: alles *doorgelaten* (HTTP 200/404), en elke aanval staat in het log. |

### Stap 2: blokkeren (DVWA)

| Zeg | Doe / laat zien |
|---|---|
| "We hebben gezien wat er binnenkomt. Nu schakelen we over naar blokkeren: één instelling." | Cockpit → **DVWA: WAF op blokkeren** (of in de GUI van FAD-FRA het profiel van `VS-DVWA-FRA` wisselen). |
| "Dezelfde SQL-injectie…" | Desktop: herhaal `1' OR '1'='1` → blokkeerpagina van de FortiADC. |
| "…en de testset: aanvallen geblokkeerd, normaal verkeer gaat door." | Cockpit → **Aanvalsset → DVWA**: `normaal verzoek` doorgelaten, aanvallen *GEBLOKKEERD*. |

### Stap 3: Juice Shop (meteen blokkeren)

| Zeg | Doe / laat zien |
|---|---|
| "De Juice Shop staat vanaf het begin op blokkeren." | Desktop: *OWASP Juice Shop*, zoek naar `<iframe src="javascript:alert(1)">` → geblokkeerd. |
| "Ook de bekende login-bypass met SQL-injectie." | Desktop: login met e-mail `' OR 1=1--` → geblokkeerd. Cockpit → **Aanvalsset → Juice Shop**. |
| "Elk incident heeft context: bron, URL, signature, actie." | GUI: Security Log → WAF → open een regel. |

### Stap 4 (optioneel): false positives en exceptions

| Zeg | Doe / laat zien |
|---|---|
| "Strenge profielen kunnen legitiem verkeer raken. Daarvoor zijn exceptions: per URL, parameter of signature." | Zoek in het log een blokkade op een normale Juice Shop-pagina (komt bij *High-Level-Security* soms voor). Laat zien hoe je een exception maakt (*WAF → Exceptions*) en hem aan het profiel hangt. |
| "Daarom: eerst monitoren, dan tunen, dan blokkeren." | Terug naar stap 1-2. |

## Achtergrond voor vragen

- De WAF-profielen in dit lab gebruiken twee bouwstenen: *Web Attack Signature* en *Heuristic SQL/XSS
  Injection Detection*. Uitbreidbaar met o.a. bot detection, brute-force-bescherming, credential stuffing
  defense, API-/OpenAPI-validatie, cookie security en data leak prevention.
- Signatures komen van FortiGuard; zonder geldige licentie/updates werkt de signature-laag met de
  meegeleverde versie.

## Terugzetten

Cockpit → **DVWA: WAF op monitoren** (of **Alles herstellen**).
