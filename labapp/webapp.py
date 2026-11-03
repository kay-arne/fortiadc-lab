#!/usr/bin/env python3
"""Demo-webapplicatie voor de realservers van het FortiADC Lab.

Eén script, alleen de standaardbibliotheek. Gedrag per container komt uit
/etc/labapp.json (naam, app, titel, kleur, datacenter); de demo-toestand uit
/var/lib/labapp/state.json, die het demo-cockpit via 'labctl' zet:

  health  ok | fail     /healthz en /api/healthz geven 200 "OK" of 503 "FAIL"
  delay_ms <n>          elke response n ms vertraagd (traag datacenter, least-connection)

Elke response draagt X-Served-By, zodat je load balancing, persistence, content
routing en GSLB-failover direct ziet in de browser, met curl of in het cockpit.
"""
from __future__ import annotations

import html
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = Path("/etc/labapp.json")
STATE = Path("/var/lib/labapp/state.json")
_hits = 0
_hits_lock = threading.Lock()


def config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {"name": socket.gethostname(), "app": "demo", "title": "Demo", "color": "#444", "dc": "?"}


def state() -> dict:
    try:
        return {"health": "ok", "delay_ms": 0, **json.loads(STATE.read_text())}
    except (OSError, ValueError):
        return {"health": "ok", "delay_ms": 0}


PRODUCTS = [("Stroopwafels (12 st.)", 4.95), ("Klompen maat 42", 39.00), ("Delfts blauw tegeltje", 12.50),
            ("Goudse kaas, belegen", 18.75), ("Tulpenbollen (25 st.)", 9.99), ("Fiets-bel, retro", 7.25)]
ORDERS = [{"id": 1001 + i, "item": p, "amount": a} for i, (p, a) in enumerate(PRODUCTS)]

PAGE = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
:root{{--c:{color}}}*{{box-sizing:border-box}}
body{{margin:0;font:15px/1.5 system-ui,sans-serif;background:#f4f6f8;color:#1c2630}}
header{{background:var(--c);color:#fff;padding:18px 28px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}}
header h1{{margin:0;font-size:22px}}.srv{{background:rgba(0,0,0,.25);border-radius:10px;padding:8px 14px;font-weight:600}}
.srv small{{display:block;font-weight:400;opacity:.85}}main{{max-width:1000px;margin:24px auto;padding:0 20px}}
.card{{background:#fff;border-radius:10px;padding:18px 22px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #e5e9ee;vertical-align:top}}
th{{width:220px;color:#5b6b7a;font-weight:600}}code{{font-size:13px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.p{{border:1px solid #e5e9ee;border-radius:8px;padding:12px}}.p b{{display:block}}.price{{color:var(--c);font-weight:700}}
.dc{{display:inline-block;background:#fff;color:var(--c);border-radius:6px;padding:0 8px;margin-left:6px;font-weight:700}}
button{{background:var(--c);color:#fff;border:0;border-radius:6px;padding:8px 14px;cursor:pointer}}
form{{display:flex;gap:8px;flex-wrap:wrap}}input{{flex:1;min-width:200px;padding:8px;border:1px solid #c9d2db;border-radius:6px}}
</style></head><body>
<header><h1>{title}</h1>
<div class="srv">Geserveerd door {name}<span class="dc">{dc}</span><small>{ip} &middot; verzoek #{hits} &middot; {now}</small></div></header>
<main>{body}
<div class="card"><h3>Hoe kwam dit verzoek binnen?</h3><table>
<tr><th>Host-header</th><td><code>{host}</code></td></tr>
<tr><th>Pad</th><td><code>{path}</code></td></tr>
<tr><th>Verbinding van</th><td><code>{peer}</code> <small>(FortiADC is een full proxy: dit is zijn interface-IP)</small></td></tr>
<tr><th>X-Forwarded-For</th><td><code>{xff}</code></td></tr>
<tr><th>Persistence-cookie</th><td><code>{cookie}</code></td></tr>
<tr><th>Health / vertraging</th><td><code>{health}</code> / <code>{delay} ms</code></td></tr>
</table>
<p><button onclick="t=!t;this.textContent=t?'Stop auto-verversen':'Auto-verversen (1 s)';tick()">Auto-verversen (1 s)</button></p>
<script>var t=false;function tick(){{if(t)setTimeout(function(){{location.reload()}},1000)}}</script>
</div></main></body></html>"""


def body_for(cfg, path) -> str:
    app = cfg.get("app")
    if app == "shop":
        items = "".join(f'<div class="p"><b>{html.escape(n)}</b><span class="price">&euro; {p:.2f}</span>'
                        f'<form method="post" action="/cart"><button>In winkelwagen</button></form></div>'
                        for n, p in PRODUCTS)
        return (f'<div class="card"><h2>Aanbiedingen</h2><div class="grid">{items}</div></div>'
                '<div class="card"><h3>Zoeken</h3><form action="/search"><input name="q" '
                'placeholder="Zoek een product"><button>Zoeken</button></form></div>')
    if app == "portal":
        return ('<div class="card"><h2>Welkom op het medewerkersportaal</h2><p>Roosters, verlof en '
                'nieuws. De <code>/api/</code>-paden van deze site worden via <b>content routing</b> door '
                'de API-servers beantwoord: probeer <a href="/api/orders">/api/orders</a>.</p></div>')
    return f'<div class="card"><h2>{html.escape(cfg.get("title", ""))}</h2><p>Pad: <code>{html.escape(path)}</code></p></div>'


class Handler(BaseHTTPRequestHandler):
    server_version = "labapp/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # journald krijgt één regel per verzoek
        print(f"{self.address_string()} {self.headers.get('Host', '-')} {fmt % args}", flush=True)

    def _send(self, code, body: str | bytes, ctype="text/html; charset=utf-8"):
        cfg = config()
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Served-By", f"{cfg['name']} ({cfg.get('dc', '?')})")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, indent=2) + "\n", "application/json")

    def handle_one(self):
        global _hits
        st = state()
        if st.get("delay_ms"):
            time.sleep(min(int(st["delay_ms"]), 30000) / 1000)
        cfg = config()
        path = self.path.split("?", 1)[0]
        with _hits_lock:
            _hits += 1
            hits = _hits
        if path in ("/healthz", "/api/healthz"):
            ok = st.get("health") != "fail"
            return self._send(200 if ok else 503, "OK\n" if ok else "FAIL\n", "text/plain")
        info = {"server": cfg["name"], "app": cfg.get("app"), "datacenter": cfg.get("dc"), "ip": cfg.get("ip"),
                "host": self.headers.get("Host"), "path": self.path, "peer": self.client_address[0],
                "x_forwarded_for": self.headers.get("X-Forwarded-For"), "request": hits}
        if path == "/whoami":
            return self._json(200, info)
        if path.startswith("/api/"):
            if path.startswith("/api/orders"):
                return self._json(200, {**info, "orders": ORDERS})
            return self._json(200, info)
        cookie = next((c.strip() for c in self.headers.get("Cookie", "").split(";") if c.strip().startswith("FADCLAB")), "-")
        body = body_for(cfg, path)
        if path == "/search":
            q = self.path.partition("q=")[2]
            body += f'<div class="card">Zoekresultaten voor <code>{html.escape(q)}</code>: geen.</div>'
        page = PAGE.format(title=html.escape(cfg.get("title", "")), color=cfg.get("color", "#444"),
                           name=html.escape(cfg["name"]), dc=html.escape(cfg.get("dc", "?")), ip=cfg.get("ip", ""),
                           hits=hits, now=time.strftime("%H:%M:%S"), body=body,
                           host=html.escape(self.headers.get("Host", "-")), path=html.escape(self.path),
                           peer=self.client_address[0], xff=html.escape(self.headers.get("X-Forwarded-For", "-")),
                           cookie=html.escape(cookie), health=st.get("health"), delay=st.get("delay_ms", 0))
        self._send(200, page)

    def do_GET(self):
        self.handle_one()

    do_HEAD = do_GET

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(min(length, 1 << 20))
        self.handle_one()


def main():
    ThreadingHTTPServer.daemon_threads = True
    port = int(os.environ.get("LABAPP_PORT", "80"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"labapp {config().get('name')} luistert op :{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
