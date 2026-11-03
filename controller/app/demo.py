"""Demo-cockpit: knoppen voor de presentator.

Alles loopt out-of-band via Proxmox (pct exec): storingen veroorzaken in een
realserver, en verkeer, DNS-vragen en aanvallen starten vanaf de client-container.
Die client staat in hetzelfde segment als de desktop en gebruikt dezelfde DNS en
CA, dus wat het cockpit laat zien is precies wat de browser van de desktop ziet.
"""
from __future__ import annotations

import shlex

import time

from . import adcconfig, fortiadc
from .config import adc, datacenter_map, domain, inventory, load_topology
from .hostctl import host


def _client(topo):
    node = next((n for n in inventory(topo) if n["role"] == "client"), None)
    if not node:
        raise RuntimeError("geen client-container in de topologie")
    return node


def _run(log, node, cmd, timeout=300):
    log(f"[{node['name']}] $ {cmd}")
    rc, out = host().pct_exec(node["vmid"], cmd, timeout=timeout)
    for line in out.rstrip().splitlines():
        log(f"  {line}")
    if rc != 0:
        log(f"  (exit {rc})")
    return rc


def webservers(topo) -> list[dict]:
    return [n for n in inventory(topo) if n["role"] == "webapp"]


def server_states() -> list[dict]:
    """Toestand van elke realserver (labctl + systemd) voor de tabel in het cockpit."""
    topo = load_topology()
    dcs = {d["segment"]: d["id"] for d in datacenter_map(topo).values()}
    out = []
    for n in webservers(topo):
        rc, txt = host().pct_exec(n["vmid"], "labctl show; systemctl is-active labapp", timeout=20)
        lines = txt.strip().splitlines()
        state = lines[0].split(": ", 1)[-1] if lines else "?"
        out.append({"name": n["name"], "app": n.get("app"), "dc": dcs.get(n["segment"], "-"), "ip": n["ip"],
                    "state": state if rc == 0 or len(lines) > 1 else "container niet bereikbaar",
                    "service": lines[-1] if len(lines) > 1 else "?"})
    return out


# --------------------------------------------------------------------------
# acties (id -> titel, functie(log, topo, arg))
# --------------------------------------------------------------------------
def _url(topo, host_, path="/"):
    return f"https://{host_}.{domain(topo)}{path}"


def traffic(log, topo, arg):
    host_, path, cookies = arg
    flag = " --cookies" if cookies else ""
    _run(log, _client(topo), f"labclient traffic{flag} -n 20 {shlex.quote(_url(topo, host_, path))}")


def dns(log, topo, arg):
    names = " ".join(f"{h}.{domain(topo)}" for h in arg)
    _run(log, _client(topo), f"for n in {names}; do labclient dns $n; done")


def dns_watch(log, topo, arg):
    log("Elke 2 seconden: wat antwoordt de GSLB? Veroorzaak intussen een storing in het cockpit.")
    _run(log, _client(topo), f"labclient dns {arg}.{domain(topo)} -n 30 --interval 2", timeout=120)


def attack(log, topo, arg):
    _run(log, _client(topo), f"labclient attack {shlex.quote(_url(topo, arg))}")


def tls(log, topo, arg):
    _run(log, _client(topo), f"labclient tls {arg}.{domain(topo)}")


def server(log, topo, arg):
    """arg = (servernaam, actie): fail | ok | slow | stop | start | reset"""
    name, what = arg
    node = next((n for n in webservers(topo) if n["name"] == name), None)
    if not node:
        raise RuntimeError(f"onbekende realserver {name}")
    cmd = {"fail": "labctl health fail", "ok": "labctl health ok", "slow": "labctl delay 2000",
           "fast": "labctl delay 0", "stop": "systemctl stop labapp && echo labapp gestopt",
           "start": "systemctl start labapp && echo labapp gestart",
           "reset": "labctl reset && systemctl start labapp"}[what]
    _run(log, node, cmd)


def datacenter(log, topo, arg):
    """arg = (DC, actie)
    down / up        : alle realservers van dat DC stoppen of herstellen (de FortiADC blijft)
    outage / restore : complete uitval: ook de FortiADC van dat DC gaat uit (en weer aan)"""
    dc, what = arg
    seg = datacenter_map(topo)[dc]["segment"]
    nodes = [n for n in webservers(topo) if n["segment"] == seg]
    a = adc(topo, dc)
    if what in ("down", "outage"):
        log(f"Datacenter {dc}: {len(nodes)} realservers UIT" + (f" en {a['hostname']} UIT" if what == "outage" else ""))
        for n in nodes:
            _run(log, n, "systemctl stop labapp && echo gestopt")
        if what == "outage":
            host().run(["qm", "stop", str(a["vmid"])], log=log)
            log(f"{a['hostname']} staat uit: hij beantwoordt geen DNS meer. De resolver vraagt het nu aan de "
                "FortiADC van het andere datacenter, die ziet dat dit DC onbereikbaar is.")
        else:
            log("De FortiADC ziet dit binnen ~15 s (health check); via auto-sync ziet de GSLB het daarna ook.")
        return
    if what == "restore" or host().status(a["vmid"]) != "running":
        host().run(["qm", "start", str(a["vmid"])], log=log, check=False)
        log(f"{a['hostname']} start; wachten tot hij weer antwoordt (een paar minuten)...")
        fortiadc.wait_reachable(log, topo, dc, 600)
    log(f"Datacenter {dc}: realservers weer AAN")
    for n in nodes:
        _run(log, n, "labctl reset && systemctl start labapp")


def gslb_status(log, topo, arg):
    for a in topo["adcs"]:
        if host().status(a["vmid"]) != "running":
            log(f"[{a['site']}] {a['hostname']} staat uit")
            continue
        try:
            fortiadc.gslb_status(log, topo, a["site"])
        except Exception as e:  # noqa: BLE001
            log(f"[{a['site']}] niet bereikbaar: {e}")


def waf_mode(log, topo, arg):
    site, vs, profile = arg
    fortiadc.set_waf_profile(log, topo, site, vs, profile)


def reset_all(log, topo, arg=None):
    log("Beide FortiADC's aan, alle realservers terug naar normaal, DVWA-WAF terug naar monitoren")
    for a in topo["adcs"]:
        if host().status(a["vmid"]) != "running":
            host().run(["qm", "start", str(a["vmid"])], log=log, check=False)
            fortiadc.wait_reachable(log, topo, a["site"], 600)
    for n in webservers(topo):
        _run(log, n, "labctl reset && systemctl start labapp")
    for vs in topo["virtual_servers"]:
        if vs.get("waf") == "WAF-LAB-MONITOR":
            for site in vs["sites"]:
                try:
                    fortiadc.set_waf_profile(log, topo, site, adcconfig.vs_name(vs, site), "WAF-LAB-MONITOR")
                except Exception as e:  # noqa: BLE001 - FortiADC hoeft hiervoor niet te draaien
                    log(f"⚠ WAF-profiel niet teruggezet: {e}")
    time.sleep(1)


# De UI bouwt het cockpit uit deze lijst; 'arg' gaat ongewijzigd terug naar de API.
CATALOG = [
    {"group": "Load balancing & persistence", "doc": "02-load-balancing.md", "actions": [
        {"id": "traffic", "arg": ["shop", "/whoami", False], "label": "20× shop zonder cookie (verdeling)"},
        {"id": "traffic", "arg": ["shop", "/whoami", True], "label": "20× shop mét cookie (persistence)"},
    ]},
    {"group": "Content routing", "doc": "05-content-routing.md", "actions": [
        {"id": "traffic", "arg": ["portal", "/", False], "label": "portal → portal-pool"},
        {"id": "traffic", "arg": ["portal", "/api/orders", False], "label": "portal/api/… → API-pool (pad)"},
        {"id": "traffic", "arg": ["api", "/", False], "label": "api → API-pool (host)"},
    ]},
    {"group": "SSL offloading", "doc": "04-ssl-offloading.md", "actions": [
        {"id": "tls", "arg": "shop", "label": "Certificaat & TLS van shop"},
        {"id": "tls", "arg": "portal", "label": "Certificaat & TLS van portal (SAN's)"},
    ]},
    {"group": "Web Application Firewall", "doc": "06-waf.md", "actions": [
        {"id": "attack", "arg": "juice", "label": "Aanvalsset → Juice Shop (AMS, blokkeren)"},
        {"id": "attack", "arg": "dvwa", "label": "Aanvalsset → DVWA (FRA)"},
        {"id": "waf_mode", "arg": ["FRA", "VS-DVWA-FRA", "WAF-LAB-BLOCK"], "label": "DVWA: WAF op blokkeren"},
        {"id": "waf_mode", "arg": ["FRA", "VS-DVWA-FRA", "WAF-LAB-MONITOR"], "label": "DVWA: WAF op monitoren"},
    ]},
    {"group": "GSLB", "doc": "07-gslb.md", "actions": [
        {"id": "dns", "arg": ["shop", "portal", "api", "juice"], "label": "DNS: wat antwoordt de lab-DNS nu?"},
        {"id": "dns_watch", "arg": "shop", "label": "Volg shop 60 s (failover live)"},
        {"id": "dns_watch", "arg": "portal", "label": "Volg portal 60 s"},
        {"id": "gslb_status", "arg": None, "label": "GSLB-status op beide FortiADC's"},
        {"id": "datacenter", "arg": ["AMS", "down"], "label": "Amsterdam: alle webservers UIT", "danger": True},
        {"id": "datacenter", "arg": ["AMS", "up"], "label": "Amsterdam: webservers AAN"},
        {"id": "datacenter", "arg": ["AMS", "outage"], "label": "Amsterdam: complete uitval (ook FortiADC)", "danger": True},
        {"id": "datacenter", "arg": ["AMS", "restore"], "label": "Amsterdam: herstellen (FortiADC + servers)"},
        {"id": "server", "arg": ["portal-ams-1", "fail"], "label": "Alleen portal in AMS laten falen", "danger": True},
        {"id": "server", "arg": ["portal-ams-1", "ok"], "label": "portal in AMS herstellen"},
    ]},
]
ACTIONS = {"traffic": traffic, "dns": dns, "dns_watch": dns_watch, "attack": attack, "tls": tls,
           "server": server, "datacenter": datacenter, "waf_mode": waf_mode, "gslb_status": gslb_status,
           "reset": reset_all}
SERVER_ACTIONS = ("fail", "ok", "slow", "fast", "stop", "start", "reset")


def allowed(action: str, arg) -> bool:
    """Alleen wat in de catalogus staat, of een serveractie op een bekende realserver."""
    if action == "reset":
        return True
    if action == "server":
        topo = load_topology()
        return (isinstance(arg, list) and len(arg) == 2 and arg[1] in SERVER_ACTIONS
                and any(n["name"] == arg[0] for n in webservers(topo)))
    return any(a["id"] == action and a["arg"] == arg for g in CATALOG for a in g["actions"])


def run(log, action: str, arg):
    ACTIONS[action](log, load_topology(), arg)
