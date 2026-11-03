"""FortiADC's: day-0 bootstrap, verbinding (SSH-CLI en REST) en provisioning.

Getest met FortiADC 8.0 (KVM); geschreven voor 7.4 en later. Elk datacenter heeft
een eigen FortiADC; alle functies krijgen het datacenter ('site', bijv. AMS) mee.

- Day-0 via de seriële console van de VM (qm terminal): inloggen in de
  fabrieksstand en de minimale CLI intypen (interfaces, route, DNS, wachtwoord).
  Een cloud-init config drive werkt op Proxmox niet voor FortiADC (getest).
- De rest gaat via SSH als gewone CLI (adcconfig.py), regel voor regel, met
  foutdetectie per regel: een onbekende optie in een andere FortiADC-build breekt
  de uitrol niet af maar komt als waarschuwing in de log.
- Certificaten, certificaatgroepen en client-SSL-profielen via de REST API
  (dezelfde endpoints als Fortinets eigen Ansible-collectie).
"""
from __future__ import annotations

import ipaddress
import re
import time

import paramiko
import requests
import urllib3

from . import adcconfig, pki
from .config import DATA, UPLOADS, adc, adc_state, get_state, update_adc_state
from .hostctl import host

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_FILE = DATA / "fortiadc-config.txt"
ADMIN_TIMEOUT = 480  # minuten; lab: lang ingelogd blijven in de GUI
# FortiADC kent tijdzones als nummer (Fortinet-lijst; 4 = Pacific, de fabrieksstand).
# 26 = (GMT+1:00) Amsterdam, Berlin, Bern, Rome, Stockholm, Vienna.
FALLBACK_ZONE = {"Amsterdam": 26}


def set_timezone(cli, log, site, tz: str):
    """Tijdzone zetten op de stad uit lab.timezone (bijv. Europe/Amsterdam). Het nummer
    wordt opgezocht in de lijst van de FortiADC zelf ('set zone ?'); lukt dat niet, dan
    de bekende Fortinet-nummering."""
    city = tz.rsplit("/", 1)[-1].replace("_", " ")
    cli.run("config system time manual")
    listing = cli.help("set zone")
    m = re.search(rf"^\s*(\d+)\b[^\n]*\b{re.escape(city)}\b", listing, re.M | re.I)
    zone = int(m.group(1)) if m else FALLBACK_ZONE.get(city)
    if zone is None:
        cli.run("end")
        log(f"[{site}] ⚠ tijdzone {tz}: niet gevonden in de zonelijst van de FortiADC; overgeslagen")
        return
    cli.run("end")
    n = cli.apply(["config system time manual", f"set zone {zone}", "set daylight-saving-time enable", "end"],
                  log, "tijdzone")
    log(f"[{site}] tijdzone {city} (zone {zone}{', uit de zonelijst' if m else ', standaardnummer'})"
        f"{'' if not n else ': NIET gezet'}")


def license_of(site) -> dict:
    """{'mode': none|file|fortiflex, 'file': ..., 'token': ...} voor deze FortiADC."""
    s = get_state()
    return {"mode": s.get("license_mode", "none"),
            "file": (s.get("license_files") or {}).get(site),
            "token": (s.get("fortiflex_tokens") or {}).get(site)}


# --------------------------------------------------------------------------
# verbinding
# --------------------------------------------------------------------------
def mgmt_ip(topo, site) -> str:
    a = adc(topo, site)
    if topo["fortiadc"]["wan"]["mode"] == "static" and a["mgmt"]:
        return str(ipaddress.ip_interface(a["mgmt"]).ip)
    if not a["mgmt_dhcp"]:
        raise RuntimeError(f"Management van {a['hostname']} staat op DHCP: vul zijn IP in de UI in.")
    return a["mgmt_dhcp"]


def base_url(topo, site) -> str:
    return f"https://{mgmt_ip(topo, site)}"


def _passwords() -> list[str]:
    """Kandidaten voor admin: het ingestelde wachtwoord, en leeg (fabrieksstand,
    als de day-0-bootstrap nog niet gedaan is)."""
    pw = get_state().get("adc_admin_password")
    return [pw, ""] if pw else [""]


def _ssh(topo, site, password=None) -> paramiko.SSHClient:
    err = None
    for pw in [password] if password is not None else _passwords():
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(mgmt_ip(topo, site), username="admin", password=pw, look_for_keys=False,
                           allow_agent=False, timeout=15, auth_timeout=20)
            client.admin_password = pw
            return client
        except paramiko.AuthenticationException as e:
            err = e
    raise err or paramiko.AuthenticationException("geen admin-wachtwoord bekend")


PROMPT = re.compile(r"[#$] $")
MORE = re.compile(r"--More--\s*$")
ASK = re.compile(r"(\(y/n\)|password:)\s*$", re.I)
# Zo meldt de FortiADC CLI een regel die hij niet accepteert.
CLI_ERROR = re.compile(r"(command parse error|parse error before|unknown action|command fail|"
                       r"entry not found|not found in datasource|invalid (input|value|ip)|"
                       r"value parse error|object .* not exist|error[: ]-?\d+)", re.I)


# Opties die per FortiADC-versie anders heten. Weigert de FortiADC een regel, dan
# probeert de controller deze schrijfwijzen (in volgorde) voor hij het als fout telt.
CLI_ALTERNATIVES = [
    # WAF-heuristiek: '...-detect' in 8.0 (getest), '...-detection' in oudere documentatie
    (re.compile(r"^set heuristic-sql-xss-injection-detect (.+)$"), [r"set heuristic-sql-xss-injection-detection \1"]),
    (re.compile(r"^set heuristic-sql-xss-injection-detection (.+)$"), [r"set heuristic-sql-xss-injection-detect \1"]),
    # GSLB-host: gewogen heet 'weight' (8.0) of 'none' (oudere documentatie)
    (re.compile(r"^set load-balance-method weight$"), ["set load-balance-method none"]),
    (re.compile(r"^set load-balance-method none$"), ["set load-balance-method weight"]),
]


def alternatives(line: str) -> list[str]:
    for pat, repls in CLI_ALTERNATIVES:
        if pat.match(line):
            return [pat.sub(r, line) for r in repls]
    return []


def _read(shell, timeout) -> str:
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if shell.recv_ready():
            buf += shell.recv(65535).decode(errors="replace")
            if MORE.search(buf):
                shell.send(" ")
                continue
            if PROMPT.search(buf) or ASK.search(buf.rstrip()):
                time.sleep(0.15)
                if not shell.recv_ready():
                    return buf
        elif shell.exit_status_ready():
            return buf
        else:
            time.sleep(0.05)
    return buf


class Cli:
    """Interactieve CLI-sessie over SSH; houdt per regel bij of hij geaccepteerd is."""

    def __init__(self, topo, site):
        self.site = site
        self.client = _ssh(topo, site)
        self.shell = self.client.invoke_shell(width=250, height=1000)
        _read(self.shell, 15)
        self.errors: list[str] = []

    def run(self, line: str, timeout=60) -> str:
        self.shell.send(line + "\n")
        out = _read(self.shell, timeout)
        if ASK.search(out.rstrip()):
            self.shell.send("y\n")
            out += _read(self.shell, timeout)
        body = out.split("\n", 1)[1] if "\n" in out else ""  # eerste regel = echo van het commando
        if CLI_ERROR.search(body):
            msg = " ".join(l.strip() for l in body.splitlines() if CLI_ERROR.search(l))[:200]
            self.errors.append(f"{line.strip()}  →  {msg}")
        return out

    def apply(self, lines: list[str], log=None, context="") -> int:
        """Voer een config-blok uit; geeft het aantal geweigerde regels terug.
        Het pad (config ... / edit ...) staat erbij, zodat je de fout terugvindt."""
        before = len(self.errors)
        path = []
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith(("config ", "edit ")):
                path.append(s)
            elif s in ("end", "next") and path:
                path.pop()
            n = len(self.errors)
            self.run(s)
            if len(self.errors) > n:
                for alt in alternatives(s):
                    m = len(self.errors)
                    self.run(alt)
                    if len(self.errors) == m:  # geaccepteerd: de oorspronkelijke fout vervalt
                        del self.errors[n:]
                        if log:
                            log(f"  ↳ '{s}' geweigerd, '{alt}' wel geaccepteerd")
                        break
                    del self.errors[m:]
            if len(self.errors) > n:
                where = " > ".join(p for p in path if p != s)
                self.errors[-1] = f"[{self.site}: {context}] {where}: {self.errors[-1]}"
                if log:
                    log(f"  ⚠ {self.errors[-1]}")
        return len(self.errors) - before

    def help(self, prefix: str, timeout=10) -> str:
        """Contexthulp van de CLI ('<prefix> ?'): de '?' werkt direct, zonder Enter.
        Daarna de invoerregel wissen (Ctrl-U), zodat er niets wordt uitgevoerd."""
        self.shell.send(prefix + " ?")
        out = _read(self.shell, timeout)
        self.shell.send("\x15")
        _read(self.shell, 2)
        return out

    def close(self):
        self.client.close()


def ssh_cli(topo, site, commands: list[str], timeout=60) -> str:
    cli = Cli(topo, site)
    try:
        return "".join(cli.run(c, timeout) for c in commands)
    finally:
        cli.close()


class Rest:
    """FortiADC REST API (token via /api/user/login, zoals de Ansible-collectie)."""

    def __init__(self, topo, site):
        self.base = base_url(topo, site)
        self.s = requests.Session()
        self.s.verify = False
        r = self.s.post(f"{self.base}/api/user/login", timeout=20,
                        json={"username": "admin", "password": get_state().get("adc_admin_password") or ""})
        r.raise_for_status()
        token = r.json().get("token")
        if not token:
            raise RuntimeError(f"REST-login gaf geen token: {r.text[:200]}")
        self.s.headers["Authorization"] = f"Bearer {token}"

    @staticmethod
    def _ok(r) -> bool:
        if not r.ok:
            return False
        try:
            p = r.json().get("payload", 0)
        except ValueError:
            return True
        return not (isinstance(p, int) and p < 0)

    def get(self, path, **params):
        r = self.s.get(f"{self.base}/api/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("payload")

    def exists(self, path, mkey) -> bool:
        try:
            p = self.get(path, mkey=mkey)
        except requests.RequestException:
            return False
        return bool(p) and not (isinstance(p, int) and p < 0)

    def upsert(self, path, mkey, body) -> tuple[bool, str]:
        if self.exists(path, mkey):
            r = self.s.put(f"{self.base}/api/{path}", params={"mkey": mkey}, json={"mkey": mkey, **body}, timeout=30)
        else:
            r = self.s.post(f"{self.base}/api/{path}", json={"mkey": mkey, **body}, timeout=30)
        return self._ok(r), r.text[:300]

    def upload_cert(self, name, cert_pem, key_pem) -> tuple[bool, str]:
        crlf = lambda s: s.replace("\r\n", "\n").replace("\n", "\r\n")  # noqa: E731 - zoals de GUI het stuurt
        fields = {"type": "CertKey", "mkey": name, "passwd": "", "vdom": "", "upload": "text",
                  "cert": crlf(cert_pem), "key": crlf(key_pem)}
        body, ctype = urllib3.encode_multipart_formdata(fields)
        r = self.s.post(f"{self.base}/api/upload/certificate_local?vdom=false", data=body,
                        headers={"Content-Type": ctype}, timeout=60)
        return self._ok(r), r.text[:300]

    def upload_license(self, lic_text: str) -> tuple[bool, str]:
        body, ctype = urllib3.encode_multipart_formdata({"vmlicense": ("license.lic", lic_text)})
        r = self.s.post(f"{self.base}/api/upload/vmlicense", data=body, headers={"Content-Type": ctype}, timeout=60)
        return self._ok(r), r.text[:300]


def api_status(topo, site) -> dict:
    """Snelle bereikbaarheidscheck voor de statusweergave."""
    try:
        r = requests.get(base_url(topo, site), verify=False, timeout=3)
        return {"ok": r.status_code < 500}
    except (requests.RequestException, RuntimeError) as e:
        return {"ok": False, "error": str(e)[:150]}


# --------------------------------------------------------------------------
# wachten en day-0
# --------------------------------------------------------------------------
def reachable(topo, site) -> bool:
    try:
        requests.get(base_url(topo, site), verify=False, timeout=5)
        return True
    except requests.RequestException:
        return False


def wait_reachable(log, topo, site, timeout, quiet=False) -> bool:
    if not quiet:
        limit = f"{timeout // 60} min" if timeout >= 60 else f"{timeout} s"
        log(f"[{site}] wachten tot {base_url(topo, site)} bereikbaar is (max {limit})...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if reachable(topo, site):
            if not quiet:
                log(f"[{site}] GUI bereikbaar")
            return True
        time.sleep(5)
    return False


def wait_reboot(log, topo, site):
    """Na een actie die herstart: eerst wachten tot hij wég is, dan tot hij terug is
    (anders ziet de controller de oude sessie nog net en gaat hij te vroeg verder)."""
    if wait_reachable(log, topo, site, 5, quiet=True):
        deadline = time.time() + 180
        while time.time() < deadline and reachable(topo, site):
            time.sleep(3)
    log(f"[{site}] herstart...")
    if not wait_reachable(log, topo, site, 900):
        raise RuntimeError(f"FortiADC {site} kwam niet terug na de herstart.")
    wait_for_ssh(log, topo, site)


def console_bootstrap(log, topo, state, site):
    """Day-0 via de seriële console (qm terminal): wacht op de login-prompt (de VM
    kan nog aan het opstarten zijn), log in en typ de bootstrap-CLI. Fabrieksstand:
    admin zonder wachtwoord; sommige builds vragen dan direct een nieuw wachtwoord."""
    h = host()
    vmid = adc(topo, site)["vmid"]
    pw = state.get("adc_admin_password") or ""
    log(f"[{site}] day-0 via de seriële console (qm terminal {vmid})")
    client = h._connect()
    chan = client.get_transport().open_session()
    chan.get_pty(width=200)
    chan.exec_command(f"{h.sudo}qm terminal {vmid} -iface serial0")

    def expect(pattern, timeout=60) -> str:
        buf, deadline = "", time.time() + timeout
        while time.time() < deadline:
            if chan.recv_ready():
                buf += chan.recv(65535).decode(errors="replace")
                if re.search(pattern, buf, re.I | re.M):
                    return buf
            else:
                time.sleep(0.2)
        raise RuntimeError(f"console: '{pattern}' niet gezien; laatste output: {buf[-300:]!r}")

    try:
        out, deadline = "", time.time() + 600
        while time.time() < deadline:  # eerste boot: kan enkele minuten duren
            chan.send("\r")
            try:
                out = expect(r"login:\s*$|# $", 15)
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError(f"console van {site}: geen login-prompt binnen 10 minuten")
        if "login" in out.lower():
            chan.send("admin\r")
            expect(r"password:\s*$", 30)
            for candidate in [pw, ""] if pw else [""]:
                chan.send(candidate + "\r")
                out = expect(r"# $|login:\s*$|new password:\s*$", 30)
                if re.search(r"new password", out, re.I):
                    chan.send(pw + "\r")
                    expect(r"(confirm|again|re-?enter).*:\s*$", 15)
                    chan.send(pw + "\r")
                    out = expect(r"# $", 30)
                if out.rstrip().endswith("#"):
                    break
                chan.send("admin\r")
                expect(r"password:\s*$", 30)
        for line in adcconfig.bootstrap_config(topo, state, site).splitlines():
            chan.send(line + "\r")
            time.sleep(0.3)
        chan.send("exit\r")
        time.sleep(1)
        chan.send("\x0f")  # Ctrl-O: qm terminal verlaten
        log(f"[{site}] bootstrap-CLI via de console ingevoerd")
    finally:
        chan.close()


def wait_for_ssh(log, topo, site, timeout=600):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            client = _ssh(topo, site)
            label = "ingesteld wachtwoord" if client.admin_password else "fabriekswachtwoord (leeg)"
            client.close()
            log(f"[{site}] SSH-login als admin gelukt ({label})")
            return
        except paramiko.AuthenticationException:
            log(f"[{site}]   SSH: wachtwoord geweigerd; 65 s wachten (lockout voorkomen)...")
            time.sleep(65)
        except (paramiko.SSHException, OSError) as e:
            if str(e) != last:
                log(f"[{site}]   SSH nog niet gelukt ({e}); opnieuw proberen...")
                last = str(e)
            time.sleep(15)
    raise RuntimeError(f"Kan niet als admin inloggen op FortiADC {site}. Controleer via de Proxmox-console of "
                       "het management-IP en het admin-wachtwoord zijn ingesteld.")


def ensure_admin_password(log, topo, site):
    """Staat admin nog op fabrieksstand (leeg), zet dan het wachtwoord uit de UI."""
    pw = get_state().get("adc_admin_password")
    if not pw:
        log(f"[{site}] ⚠ geen admin-wachtwoord ingesteld in de UI; admin blijft zonder wachtwoord")
        return
    try:
        _ssh(topo, site, pw).close()
        return
    except paramiko.AuthenticationException:
        pass
    log(f"[{site}] admin-wachtwoord instellen (fabriekswachtwoord vervangen)")
    cli = Cli(topo, site)
    try:
        cli.apply(["config system admin", 'edit "admin"', f"set password {pw}", "next", "end"], log, "admin")
    finally:
        cli.close()
    _ssh(topo, site, pw).close()
    log(f"[{site}] ✔ admin-wachtwoord ingesteld en getest")


def license_status(log, topo, site):
    """Een FortiADC-VM zonder licentie heeft het serienummer FADV0000000TRIAL; met
    licentie het echte serienummer. Dat is betrouwbaarder dan een 'License'-regel,
    want die verschilt per versie (8.0 heeft hem niet in 'get system status')."""
    out = ssh_cli(topo, site, ["get system status"])
    m = re.search(r"^\s*(?:VM )?Licen[cs]e(?: Status)?\s*:\s*(.+)$", out, re.I | re.M)
    serial = re.search(r"^\s*Serial-Number\s*:\s*(\S+)", out, re.I | re.M)
    ver = re.search(r"^\s*Version\s*:\s*(.+)$", out, re.I | re.M)
    serial = serial.group(1) if serial else ""
    licensed = bool(serial) and "TRIAL" not in serial.upper()
    if m and re.search(r"invalid|expired", m.group(1), re.I):
        licensed = False
    info = {"license": "geldig" if licensed else ("evaluatie" if serial else "onbekend"), "serial": serial,
            "version": ver.group(1).strip() if ver else "", "licensed": licensed}
    update_adc_state(site, info=info)
    log(f"[{site}] FortiADC {info['version'] or '?'} serienummer {serial or '?'}, licentie: {info['license']}")
    return info


def ensure_license(log, topo, site):
    info = license_status(log, topo, site)
    if info["licensed"]:
        return
    lic = license_of(site)
    if lic["mode"] == "file" and lic["file"]:
        log(f"[{site}] nog geen licentie; licentiebestand via REST uploaden (de FortiADC herstart)")
        ok, detail = Rest(topo, site).upload_license((UPLOADS / lic["file"]).read_text())
        log(f"[{site}]   upload: {'ok' if ok else 'mislukt'} {'' if ok else detail}")
        if not ok:
            return
    elif lic["mode"] == "fortiflex" and lic["token"]:
        log(f"[{site}] FortiFlex-token activeren (execute vm-license); de FortiADC herstart daarna")
        out = ssh_cli(topo, site, [f"execute vm-license {lic['token']}"], timeout=120)
        log(f"[{site}]   " + " ".join(out.split())[-200:])
        if re.search(r"fail|error|invalid", out, re.I) and not re.search(r"rebooting", out, re.I):
            # Geen herstart: de FortiADC weigerde het token (al gebruikt, verlopen of ongeldig).
            log(f"[{site}] ⚠ token geweigerd; controleer het token in het FortiFlex-portal. Verder zonder licentie.")
            return
    else:
        log(f"[{site}] ⚠ geen licentie: de FortiADC-VM draait in evaluatiemodus (beperkt).")
        return
    wait_reboot(log, topo, site)
    after = license_status(log, topo, site)
    if not after["licensed"]:
        log(f"[{site}] ⚠ nog steeds geen geldige licentie (serienummer {after['serial'] or '?'}); "
            "controleer het token of licentiebestand")


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------
def upload_certificates(log, topo, site) -> list[str]:
    """Per virtual server van dit DC: certificaat (lab-CA) → certificaatgroep → client SSL profile."""
    pki.ensure_ca(topo)
    rest = Rest(topo, site)
    problems = []
    for vs in adcconfig.site_vses(topo, site):
        cert, group, profile = adcconfig.ssl_names(vs)
        names = pki.vs_names(topo, vs)
        crt, key = pki.issue(topo, cert, names)
        if rest.exists("system_certificate_local", cert):
            log(f"[{site}]   {cert}: bestaat al")
        else:
            ok, detail = rest.upload_cert(cert, crt, key)
            log(f"[{site}]   {cert} ({', '.join(names)}): {'geüpload' if ok else 'upload mislukt: ' + detail}")
            if not ok:
                problems.append(f"[{site}] certificaat {cert}: {detail}")
        ok, detail = rest.upsert("system_certificate_local_cert_group", group, {})
        members = rest.get("system_certificate_local_cert_group_child_group_member", pkey=group) or []
        if ok and not any(isinstance(m, dict) and m.get("local_cert") == cert for m in members):
            r = rest.s.post(f"{rest.base}/api/system_certificate_local_cert_group_child_group_member",
                            params={"pkey": group}, json={"local_cert": cert, "default": "enable"}, timeout=30)
            ok, detail = rest._ok(r), r.text[:300]
        if not ok:
            problems.append(f"[{site}] certificaatgroep {group}: {detail}")
            log(f"[{site}]   ⚠ {group}: {detail}")
        # TLS 1.2+ (best practice); valt de versie-optie weg in deze build, dan zonder.
        ok, detail = rest.upsert("load_balance_client_ssl_profile", profile,
                                 {"local_certificate_group": group, "ssl-allowed_versions": "tlsv1.2 tlsv1.3"})
        if not ok:
            ok, detail = rest.upsert("load_balance_client_ssl_profile", profile, {"local_certificate_group": group})
        log(f"[{site}]   {profile} → {group}: {'ok' if ok else detail}")
        if not ok:
            problems.append(f"[{site}] client SSL profile {profile}: {detail}")
    return problems


def table_entries(cli: Cli, path: str) -> list[str]:
    """Namen in een CLI-tabel, ook de voorgedefinieerde ('show' of anders 'get')."""
    names = []
    for cmd in (f"show {path}", f"get {path}"):
        out = cli.run(cmd, timeout=30)
        names += re.findall(r'edit "([^"]+)"', out) + re.findall(r"==\s*\[\s*([^\]]+?)\s*\]", out)
        if names:
            break
    return list(dict.fromkeys(names))


# Welk vooraf gedefinieerd object bij welk niveau hoort. De namen verschillen per
# FortiADC-versie (7.x: High-Level-Security / Alert-Only); daarom lezen we de lijst
# op de FortiADC zelf en kiezen we op trefwoord.
WAF_LEVEL_HINTS = {"High-Level-Security": ("high", "block", "strict"),
                   "Alert-Only": ("alert", "monitor", "detect")}
WAF_OBJECTS = {"web-attack-signature": "security waf web-attack-signature",
               "heuristic-sql-xss-injection-detect": "security waf heuristic-sql-xss-injection-detection"}


def resolve_waf(cli: Cli, lines: list[str], log, site) -> list[str]:
    """Vervang de WAF-niveaus in het profielblok door objecten die echt bestaan;
    bestaat er geen passend object, laat de regel dan weg (met een melding)."""
    available = {opt: table_entries(cli, path) for opt, path in WAF_OBJECTS.items()}
    for opt, names in available.items():
        log(f"[{site}]   beschikbaar voor {opt}: {', '.join(names) or 'geen'}")
    out = []
    for line in lines:
        m = re.match(r"(\s*)set (\S+) (\S+)\s*$", line)
        if not m or m.group(2) not in available:
            out.append(line)
            continue
        indent, opt, level = m.groups()
        names = available[opt]
        if level in names:
            out.append(line)
            continue
        hints = WAF_LEVEL_HINTS.get(level, ())
        pick = next((n for h in hints for n in names if h in n.lower()), None)
        if pick:
            log(f"[{site}]   {opt}: '{level}' bestaat niet, '{pick}' gebruikt")
            out.append(f"{indent}set {opt} {pick}")
        else:
            log(f"[{site}]   ⚠ {opt}: geen object voor '{level}' op deze FortiADC; regel overgeslagen")
    return out


def gslb_zones(cli: Cli) -> list[str]:
    out = cli.run("show global-dns-server zone", timeout=30)
    return sorted(set(re.findall(r'edit "([^"]+)"', out)))


def prepare(log, topo, site):
    """Bereikbaar, ingelogd, gelicenseerd. Is het management-IP nog niet bereikbaar,
    dan is de FortiADC nieuw (of de bootstrap mislukte): dan via de console."""
    if not wait_reachable(log, topo, site, 20):
        console_bootstrap(log, topo, get_state(), site)
        if not wait_reachable(log, topo, site, 300):
            raise RuntimeError(f"FortiADC {site} niet bereikbaar op het management-IP. Controleer port1 "
                               "(bridge/VLAN) en het IP via de Proxmox-console.")
    wait_for_ssh(log, topo, site)
    ensure_admin_password(log, topo, site)
    ensure_license(log, topo, site)


def provision_slb(log, topo, site) -> list[str]:
    log(f"== [{site}] certificaten (lab-CA) en client-SSL-profielen via REST ==")
    problems = upload_certificates(log, topo, site)
    cli = Cli(topo, site)
    try:
        cli.apply(["config system global", f"set admin-idle-timeout {ADMIN_TIMEOUT}", "end"], log, "global")
        set_timezone(cli, log, site, topo["lab"].get("timezone", "Europe/Amsterdam"))
        for title, lines in adcconfig.slb_blocks(topo, site):
            log(f"== [{site}] {title} ({sum(1 for l in lines if l.strip())} regels) ==")
            if title == "WAF-profielen":
                lines = resolve_waf(cli, lines, log, site)
            n = cli.apply(lines, log, title)
            log(f"[{site}]   {'✔ toegepast' if not n else f'⚠ {n} regel(s) geweigerd'}")
    finally:
        cli.close()
    return problems + cli.errors


def provision_gslb(log, topo, site) -> list[str]:
    cli = Cli(topo, site)
    try:
        for title, lines in adcconfig.gslb_blocks(topo, site):
            log(f"== [{site}] {title} ==")
            n = cli.apply(lines, log, title)
            log(f"[{site}]   {'✔ toegepast' if not n else f'⚠ {n} regel(s) geweigerd'}")
        n = len(cli.errors)
        for line in adcconfig.gslb_cleanup(site):  # bestaat hij niet, dan is dat prima
            cli.run(line)
        del cli.errors[n:]
        time.sleep(3)
        zones = gslb_zones(cli)
        log(f"[{site}] GSLB-zones (gegenereerd door de hosts): {', '.join(zones) or 'geen'}")
        if zones:
            cli.apply(adcconfig.dns_policy(zones), log, "DNS-policy")
        else:
            cli.errors.append(f"[{site}: DNS-policy] geen gegenereerde zone; controleer Global Load Balance > Hosts")
    finally:
        cli.close()
    return cli.errors


def provision(log, topo, sites=None):
    """Eerst SLB op alle gekozen FortiADC's, daarna GSLB: auto-sync haalt de virtual
    servers van de andere FortiADC op, dus die moeten er dan al staan."""
    sites = sites or [a["site"] for a in topo["adcs"]]
    CONFIG_FILE.write_text(adcconfig.render(topo))
    problems = {s: [] for s in sites}
    for site in sites:
        prepare(log, topo, site)
        problems[site] += provision_slb(log, topo, site)
    if adcconfig.gslb_vses(topo):
        for site in sites:
            problems[site] += provision_gslb(log, topo, site)
    for site in sites:
        update_adc_state(site, provisioned=True, problems=problems[site], provisioned_at=time.time())
    total = sum(len(p) for p in problems.values())
    if total:
        log(f"⚠ provisioning klaar met {total} waarschuwing(en); zie hierboven. De rest van de config staat.")
    else:
        log("✔ FortiADC's volledig geprovisioned")


def gslb_status(log, topo, site):
    """Wat ziet deze FortiADC van de GSLB (servers en hun virtual servers)?"""
    cli = Cli(topo, site)
    try:
        for cmd in ("get global-load-balance servers", "show global-load-balance host"):
            out = cli.run(cmd, timeout=30)
            log(f"[{site}] $ {cmd}")
            for line in out.splitlines()[1:]:
                if line.strip() and not PROMPT.search(line + " "):
                    log(f"  {line.rstrip()}")
    finally:
        cli.close()


def set_waf_profile(log, topo, site, vs_name: str, profile: str):
    cli = Cli(topo, site)
    try:
        n = cli.apply(["config load-balance virtual-server", f'edit "{vs_name}"', f"set waf-profile {profile}",
                       "next", "end"], log, "WAF")
    finally:
        cli.close()
    log(f"[{site}] {vs_name}: WAF-profiel {profile} {'actief' if not n else 'NIET gezet'}")


def problems_all(topo) -> list[str]:
    return [p for a in topo["adcs"] for p in adc_state(a["site"]).get("problems") or []]
