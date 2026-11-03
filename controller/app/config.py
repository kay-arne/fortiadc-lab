"""Paden, topologie en persistente state van de controller."""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import string
import threading
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("ADCLAB_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
PKI = DATA / "pki"                  # lab-CA en servercertificaten (niet in git)
TOPOLOGY_FILE = ROOT / "lab" / "topology.yaml"
STATE_FILE = DATA / "state.json"
HOST_FILE = DATA / "host.json"

for _d in (DATA, UPLOADS, PKI):
    _d.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def load_topology() -> dict:
    with open(TOPOLOGY_FILE) as f:
        topo = yaml.safe_load(f)
    # Keuzes uit de installer (host.json) overschrijven de defaults uit de YAML.
    hcfg = load_host() if HOST_FILE.exists() else {}
    lab = topo["lab"]
    lab["uplink_bridge"] = hcfg.get("uplink_bridge") or lab["uplink_bridge"]
    lab["uplink_vlan"] = str(hcfg.get("uplink_vlan") or lab.get("uplink_vlan") or "")
    lab["template_net"] = hcfg.get("template_net") or lab.get("template_net", "ip=dhcp")
    lab["name_prefix"] = hcfg.get("name_prefix") or lab.get("name_prefix") or "adclab"
    if hcfg.get("vmid_base"):
        lab["vmid_base"] = int(hcfg["vmid_base"])
    topo["desktop"]["vmid"] = lab["vmid_base"] + 5
    # Management-instellingen uit de UI overschrijven de defaults uit de YAML.
    state = get_state()
    topo["fortiadc"]["wan"].update(state.get("wan", {}))
    topo["adcs"] = _adcs(topo, state)
    return topo


ADC_VMID_OFFSET = (0, 2)  # FAD-AMS = base, FAD-FRA = base+2 (base+1 is het template)


def _adcs(topo: dict, state: dict) -> list[dict]:
    """Eén FortiADC per datacenter: VMID, naam, management-IP en lab-poorten."""
    out = []
    for i, dc in enumerate(topo["datacenters"]):
        site = dc["id"]
        ports = sorted(((s["adc"][site]["port"], s["bridge"],
                         f"{s['adc'][site]['ip']}/{ipaddress.ip_network(s['subnet']).prefixlen}", s["id"])
                        for s in topo["segments"] if site in s.get("adc", {})), key=lambda p: int(p[0][4:]))
        out.append({"site": site, "dc": dc["name"], "name": dc["adc"],
                    "vmid": topo["lab"]["vmid_base"] + ADC_VMID_OFFSET[i],
                    "hostname": vm_name(topo, dc["adc"]),
                    "mgmt": (state.get("adc_ips") or {}).get(site, ""),
                    "mgmt_dhcp": (state.get("adc_mgmt_ip") or {}).get(site, ""),
                    "ports": [{"port": p, "bridge": b, "ip": ip, "segment": seg} for p, b, ip, seg in ports]})
    return out


def adc(topo: dict, site: str) -> dict:
    return next(a for a in topo["adcs"] if a["site"] == site)


def adc_state(site: str) -> dict:
    """Provisioning-toestand per FortiADC (info, waarschuwingen)."""
    return (get_state().get("adc") or {}).get(site, {})


def update_adc_state(site: str, **changes):
    with _lock:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        state.setdefault("adc", {}).setdefault(site, {}).update(changes)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)


def vm_name(topo: dict, name: str) -> str:
    """Naam van een VM/container in Proxmox: <prefix>-<naam>, bijv. adclab-shop-ams-1."""
    return f"{topo['lab']['name_prefix']}-{name}"


def uplink_tag(topo: dict) -> str:
    """',tag=<vlan>' voor net-opties op de uplink, of leeg."""
    vlan = topo["lab"].get("uplink_vlan")
    return f",tag={vlan}" if vlan else ""


def load_host() -> dict:
    with open(HOST_FILE) as f:
        return json.load(f)


def get_state() -> dict:
    with _lock:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text())


def update_state(**changes) -> dict:
    with _lock:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        state.update(changes)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)
        return state


def generated_secret(key: str, length: int = 14) -> str:
    """Eenmalig gegenereerd wachtwoord (bewaard in state.json); dit is een lab,
    de UI toont het leesbaar achter de login."""
    value = get_state().get(key)
    if not value:
        value = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))
        update_state(**{key: value})
    return value


# --- netwerk en namen ----------------------------------------------------------
def segment_map(topo: dict) -> dict:
    return {s["id"]: s for s in topo["segments"]}


def prefixlen(subnet: str) -> int:
    return ipaddress.ip_network(subnet).prefixlen


def netmask(subnet: str) -> str:
    return str(ipaddress.ip_network(subnet).netmask)


def domain(topo: dict) -> str:
    return topo["dns"]["domain"]


def gslb_domain(topo: dict) -> str:
    return f"{topo['dns']['gslb_subdomain']}.{topo['dns']['domain']}"


def fqdn(topo: dict, host: str) -> str:
    return f"{host}.{domain(topo)}"


def datacenter_map(topo: dict) -> dict:
    return {d["id"]: d for d in topo["datacenters"]}


def inventory(topo: dict) -> list[dict]:
    """Alle LXC's met vaste VMID's (base+10 en verder, in volgorde van de YAML)."""
    base = topo["lab"]["vmid_base"]
    return [{**n, "kind": "node", "vmid": base + 10 + i, "args": n.get("args", {}),
             "hostname": vm_name(topo, n["name"])}
            for i, n in enumerate(topo["nodes"])]


def node_by_name(topo: dict, name: str) -> dict | None:
    return next((n for n in inventory(topo) if n["name"] == name), None)


def pool_members(topo: dict, app: str, site: str) -> list[dict]:
    """Realservers van een app in het segment van een datacenter."""
    seg = datacenter_map(topo)[site]["segment"]
    return [n for n in inventory(topo) if n.get("app") == app and n["segment"] == seg]


def template_vmid(topo: dict) -> int:
    return topo["lab"]["vmid_base"] + 1
