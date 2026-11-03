# 9. Zichtbaarheid: FortiView en logs

**Kernboodschap:** de FortiADC weet van elke applicatie wie er komt, hoe snel het antwoord is, welke server
het deed en welke aanvallen zijn tegengehouden. Dat is waardevol voor operations én security.

**Duur:** 5 minuten · **Voorwaarde:** alle virtual servers hebben *traffic log* en *FortiView* aan (staat
in de gegenereerde config).

## Demo

| Zeg | Doe / laat zien |
|---|---|
| "Eerst wat verkeer." | Cockpit → **20× shop zonder cookie**, **portal → portal-pool**, **Aanvalsset → Juice Shop**. |
| "FortiView: in één oogopslag welke virtual servers het drukst zijn en welke clients het meest verkeer maken." | GUI: *FortiView* → per virtual server / per client. |
| "Het traffic log: per verzoek de VS, de gekozen realserver, de responstijd." | *Log & Report → Traffic Log*: filter op `VS-SHOP-AMS`. |
| "Het security log: elke WAF-gebeurtenis met signature en actie." | *Log & Report → Security Log → WAF*. |
| "En GSLB: welke DNS-vragen beantwoord zijn en met welk datacenter." | Lab-DNS: de query-log staat in de DNS-container (`/var/log/named/queries.log`). |

## Uitbreiding

- Logs doorsturen naar FortiAnalyzer of een SIEM (syslog) voor correlatie met andere Fortinet-producten.
- Alerts via e-mail/SNMP bij een pool zonder gezonde servers.
