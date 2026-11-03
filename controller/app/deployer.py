"""Uitrol en afbraak van het lab op de Proxmox host."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import requests
import yaml

from . import fortiadc, pki, updates
from .config import (ROOT, UPLOADS, adc, adc_state, datacenter_map, domain, generated_secret, get_state, gslb_domain,
                     inventory, load_host, load_topology, prefixlen, segment_map, template_vmid, update_adc_state,
                     update_state, uplink_tag, vm_name)
from .hostctl import HostError, host

DESKTOP_ISO = "adclab-desktop-seed.iso"
# Het FortiADC KVM-pakket bevat twee images; ze worden onder vaste namen bewaard.
ADC_IMAGES = {"boot": "fortiadc-boot.qcow2", "data": "fortiadc-data.qcow2"}
APT_PACKAGES = ("python3 curl ca-certificates openssl dnsutils bind9 bind9-utils docker.io tcpdump iproute2 "
                "iputils-ping netcat-openbsd apache2-utils")
CA_PATH = "/usr/local/share/ca-certificates/adclab-root-ca.crt"


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def lab_status() -> dict:
    topo = load_topology()
    h = host()
    out = h.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"])
    present = {r["vmid"]: r for r in json.loads(out)}

    def st(vmid):
        r = present.get(vmid)
        return r.get("status", "unknown") if r else "absent"

    links = set(h.run("ls /sys/class/net").split())
    adcs = []
    for a in topo["adcs"]:
        status = st(a["vmid"])
        reach = fortiadc.api_status(topo, a["site"]) if status == "running" else {"ok": False}
        ast = adc_state(a["site"])
        adcs.append({"site": a["site"], "dc": a["dc"], "vmid": a["vmid"], "status": status, "name": a["hostname"],
                     "mgmt": a["mgmt"] or a["mgmt_dhcp"] or topo["fortiadc"]["wan"]["mode"],
                     "ports": a["ports"], "reachable": reach["ok"], "provisioned": ast.get("provisioned", False),
                     "info": ast.get("info") or {}, "problems": len(ast.get("problems") or [])})
    return {
        "adcs": adcs,
        "template": {"vmid": template_vmid(topo), "status": st(template_vmid(topo))},
        "desktop": {"vmid": topo["desktop"]["vmid"], "status": st(topo["desktop"]["vmid"]),
                    "name": vm_name(topo, topo["desktop"]["name"]), "ip": topo["desktop"]["lab_ip"]},
        "segments": [{**seg, "present": seg["bridge"] in links} for seg in topo["segments"]],
        "nodes": [{**n, "status": st(n["vmid"])} for n in inventory(topo)],
    }


# --------------------------------------------------------------------------
# netwerk
# --------------------------------------------------------------------------
def ensure_bridges(log, topo):
    h = host()
    links = set(h.run("ls /sys/class/net").split())
    created = False
    for seg in topo["segments"]:
        if seg["bridge"] in links:
            log(f"bridge {seg['bridge']} bestaat al ({seg['name']})")
            continue
        log(f"bridge {seg['bridge']} aanmaken voor {seg['name']} (geen fysieke poort, geen host-IP)")
        h.run(["pvesh", "create", f"/nodes/{h.node}/network", "--iface", seg["bridge"],
               "--type", "bridge", "--autostart", "1", "--comments", f"adclab {seg['name']}"])
        created = True
    if created:
        log("netwerkconfig toepassen (ifreload)")
        h.run(["pvesh", "set", f"/nodes/{h.node}/network"])


def remove_bridges(log, topo):
    h = host()
    links = set(h.run("ls /sys/class/net").split())
    removed = False
    for seg in topo["segments"]:
        if seg["bridge"] in links:
            log(f"bridge {seg['bridge']} verwijderen")
            h.run(["pvesh", "delete", f"/nodes/{h.node}/network/{seg['bridge']}"], check=False)
            removed = True
    if removed:
        h.run(["pvesh", "set", f"/nodes/{h.node}/network"])


def _put_with_progress(log, local: Path, remote: str):
    h = host()
    if h.run(f"test -f {shlex.quote(remote)} && stat -c %s {shlex.quote(remote)}", check=False).strip() \
            == str(local.stat().st_size):
        return
    log(f"{local.name} naar de host kopiëren...")
    last = [0]

    def progress(done, total):
        pct = int(done * 100 / total) if total else 100
        if pct >= last[0] + 20:
            last[0] = pct
            log(f"  {pct}%")
    h.put(local, remote, callback=progress)


def _install_iso(local: Path, name: str):
    """ISO via de werkmap naar local:iso (sudo-regel staat alleen deze namen toe)."""
    h = host()
    h.put(local, f"{h.workdir}/{name}")
    h.run(["install", "-m", "0644", f"{h.workdir}/{name}", f"/var/lib/vz/template/iso/{name}"])


def _import_boot_disk(log, vmid, image, storage, bus, opts="", index=0):
    """qcow2 importeren (komt binnen als unused0) en koppelen als <bus><index>."""
    h = host()
    try:
        h.run(["qm", "disk", "import", str(vmid), image, storage], log=log)
    except HostError:
        h.run(["qm", "importdisk", str(vmid), image, storage], log=log)  # oudere PVE-versies
    unused = re.search(r"^unused0:\s*(\S+)", h.run(["qm", "config", str(vmid)]), re.M)
    if not unused:
        raise RuntimeError("geïmporteerde disk niet gevonden in de VM-config")
    h.run(["qm", "set", str(vmid), f"--{bus}{index}", unused.group(1) + opts], log=log)


def node_mac(ip: str, oui="02:AD:C0") -> str:
    """Vast, lokaal beheerd MAC-adres per IP: na een herinstallatie blijft een
    server hetzelfde in ARP-tabellen en logs."""
    o = [int(x) for x in ip.split(".")]
    return oui + ":" + ":".join(f"{x:02X}" for x in o[1:])


# --------------------------------------------------------------------------
# FortiADC
# --------------------------------------------------------------------------
def deploy_fortiadc(log, topo, site):
    h = host()
    hcfg = load_host()
    state = get_state()
    fad = topo["fortiadc"]
    a = adc(topo, site)
    vmid = a["vmid"]
    images = {kind: state.get(f"adc_{kind}_image") for kind in ADC_IMAGES}
    missing = [kind for kind, name in images.items() if not name or not (UPLOADS / name).exists()]
    if missing:
        raise RuntimeError(f"FortiADC-image ontbreekt: {', '.join(f'{k}.qcow2' for k in missing)} (upload beide "
                           "bestanden uit de KVM-zip).")
    if fad["wan"]["mode"] == "static" and not a["mgmt"]:
        raise RuntimeError(f"Stel eerst het management-IP van {a['hostname']} in.")
    if not state.get("adc_admin_password"):
        raise RuntimeError("Geen FortiADC admin-wachtwoord ingesteld.")
    if h.exists(vmid):
        log(f"{a['hostname']} ({vmid}) bestaat al, overslaan")
        return
    h.run(["mkdir", "-p", h.workdir])
    remote = {}
    for kind, name in images.items():
        remote[kind] = f"{h.workdir}/{name}"
        _put_with_progress(log, UPLOADS / name, remote[kind])
    log(f"[{site}] cloud-init config drive bouwen (day-0 CLI + licentie)")
    _install_iso(fortiadc.build_config_iso(topo, state, site), fortiadc.iso_name(site))

    update_adc_state(site, provisioned=False, problems=[], info=None)
    bus = fad.get("disk_bus", "virtio")
    nics = [topo["lab"]["uplink_bridge"] + uplink_tag(topo)] + [p["bridge"] for p in a["ports"]]
    desc = ", ".join(f"{p['port']} = {segment_map(topo)[p['segment']]['name']}" for p in a["ports"])
    args = ["qm", "create", str(vmid), "--name", a["hostname"], "--ostype", "l26", "--cpu", "host",
            "--cores", str(fad["cores"]), "--memory", str(fad["memory"]), "--scsihw", "virtio-scsi-single",
            "--serial0", "socket", "--tags", f"adclab;{site.lower()}", "--onboot", "1",
            "--description", f"FortiADC-VM datacenter {a['dc']} (FortiADC Lab). port1 = management, {desc}"]
    for i, bridge in enumerate(nics):
        args += [f"--net{i}", f"virtio,bridge={bridge},firewall=0"]
    try:
        log(f"{a['hostname']} ({vmid}) aanmaken ({len(nics)} NIC's)")
        h.run(args, log=log)
        # Zoals in Fortinets KVM-installatiegids: boot.qcow2 als eerste en data.qcow2
        # als tweede VirtIO-disk. De datadisk (logs, rapporten) wordt vergroot.
        _import_boot_disk(log, vmid, remote["boot"], hcfg["storage"], bus, index=0)
        _import_boot_disk(log, vmid, remote["data"], hcfg["storage"], bus, index=1)
        if fad.get("data_disk_gb"):
            h.run(["qm", "disk", "resize", str(vmid), f"{bus}1", f"{fad['data_disk_gb']}G"], log=log)
        h.run(["qm", "set", str(vmid), "--ide2", f"local:iso/{fortiadc.iso_name(site)},media=cdrom"], log=log)
        h.run(["qm", "set", str(vmid), "--boot", f"order={bus}0"], log=log)
    except Exception:
        log(f"aanmaken mislukt; half-aangemaakte VM {vmid} opruimen")
        h.run(["qm", "destroy", str(vmid), "--purge", "1"], check=False)
        raise
    h.run(["qm", "start", str(vmid)], log=log)
    log(f"{a['hostname']} start; de eerste boot duurt een paar minuten")


# --------------------------------------------------------------------------
# LXC-template
# --------------------------------------------------------------------------
def _labapp_tarball() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="labapp-")) / "labapp.tgz"
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(ROOT / "labapp", arcname="labapp", filter=lambda t: None if "__pycache__" in t.name else t)
    return tmp


def push_labapp(log, vmid):
    h = host()
    h.put(_labapp_tarball(), f"{h.workdir}/labapp.tgz")
    h.run(["pct", "push", str(vmid), f"{h.workdir}/labapp.tgz", "/tmp/labapp.tgz"])
    h.pct_exec(vmid, "rm -rf /opt/labapp && tar xzf /tmp/labapp.tgz -C /opt && "
                     "install -m755 /opt/labapp/labctl /usr/local/bin/labctl && "
                     "install -m755 /opt/labapp/labclient.py /usr/local/bin/labclient && "
                     "install -m644 /opt/labapp/labapp.service /etc/systemd/system/labapp.service && "
                     "systemctl daemon-reload", check=True)


def _is_template(h, vmid) -> bool:
    return bool(re.search(r"^template:\s*1", h.run(["pct", "config", str(vmid)], check=False), re.M))


def build_template(log, topo):
    h = host()
    hcfg = load_host()
    vmid = template_vmid(topo)
    if h.exists(vmid):
        if _is_template(h, vmid):
            log(f"template {vmid} bestaat al, overslaan")
            return
        log(f"container {vmid} is een onvoltooid template; opnieuw bouwen")
        h.run(["pct", "stop", str(vmid)], check=False)
        h.run(["pct", "destroy", str(vmid), "--purge", "1"], log=log)
    tnet = topo["lab"]["template_net"]
    bridge = f"{topo['lab']['uplink_bridge']}{uplink_tag(topo)}"
    log(f"template-LXC {vmid} bouwen (tijdelijk op {bridge.replace(',tag=', ' VLAN ')}, {tnet}, voor packages)")
    # nesting + keyctl: nodig voor Docker (Juice Shop, DVWA) in een unprivileged container.
    h.run(["pct", "create", str(vmid), hcfg["template"], "--hostname", vm_name(topo, "template"),
           "--cores", "2", "--memory", "1024", "--rootfs", f"{hcfg['storage']}:8",
           "--unprivileged", "1", "--features", "nesting=1,keyctl=1",
           "--net0", f"name=eth0,bridge={bridge},{tnet}", "--tags", "adclab"], log=log)
    try:
        h.run(["pct", "start", str(vmid)], log=log)
        wait_for_internet(log, h, vmid)
        log("apt packages installeren (labapp, BIND, Docker, tools)")
        h.pct_exec(vmid, f"export DEBIAN_FRONTEND=noninteractive; apt-get update -q && apt-get -yq upgrade && "
                         f"apt-get install -yq {APT_PACKAGES}", timeout=1800, check=True)
        tz = topo["lab"].get("timezone", "Europe/Amsterdam")
        h.pct_exec(vmid, f"ln -sf /usr/share/zoneinfo/{tz} /etc/localtime && echo {tz} > /etc/timezone")
        push_labapp(log, vmid)
        # Diensten draaien alleen waar hun rol ze nodig heeft.
        h.pct_exec(vmid, "systemctl disable --now named docker docker.socket containerd 2>/dev/null; "
                         "apt-get clean; truncate -s0 /etc/machine-id")
        log("netwerk loskoppelen en converteren naar template")
        h.run(["pct", "stop", str(vmid)])
        h.run(["pct", "set", str(vmid), "--delete", "net0"])
        h.run(["pct", "template", str(vmid)])
    except Exception:
        log(f"template-build mislukt; container {vmid} opruimen")
        h.run(["pct", "stop", str(vmid)], check=False)
        h.run(["pct", "destroy", str(vmid), "--purge", "1"], check=False)
        raise


def wait_for_internet(log, h, vmid, timeout=180):
    log("wachten op IP-adres en DNS...")
    start = time.time()
    while time.time() - start < timeout:
        if h.pct_exec(vmid, "getent hosts deb.debian.org")[0] == 0:
            log(f"internet OK na {int(time.time() - start)}s")
            return
        time.sleep(3)
    _, diag = h.pct_exec(vmid, "ip -4 -br addr; ip route; cat /etc/resolv.conf")
    raise RuntimeError(f"container {vmid} heeft geen internet via de uplink (DHCP/VLAN/template_net?).\n{diag}")


# --------------------------------------------------------------------------
# DNS (BIND)
# --------------------------------------------------------------------------
def bind_config(topo) -> dict[str, str]:
    """named.conf.options en de zone voor het lab-domein, met delegatie van het
    GSLB-subdomein naar de FortiADC."""
    dns_node = next(n for n in inventory(topo) if n["name"] == topo["dns"]["server"])
    acl = " ".join(f"{s['subnet']};" for s in topo["segments"])
    options = f"""// Gegenereerd door de FortiADC Lab controller.
acl lab {{ {acl} localhost; }};

options {{
    directory "/var/cache/bind";
    listen-on {{ 127.0.0.1; {dns_node['ip']}; }};
    listen-on-v6 {{ none; }};
    allow-query {{ lab; }};
    recursion yes;                  // volgt de delegatie naar de FortiADC (GSLB)
    allow-recursion {{ lab; }};
    allow-transfer {{ none; }};
    // Het lab heeft geen internet; zonder bereikbare root kan BIND niets valideren.
    dnssec-validation no;
    // Korte cache: GSLB-failover is direct zichtbaar in de demo.
    max-cache-ttl 5;
    max-ncache-ttl 5;
    querylog yes;
    version "not disclosed";
}};

logging {{
    channel queries {{ file "/var/log/named/queries.log" versions 3 size 5m; print-time yes; }};
    category queries {{ queries; }};
    category lame-servers {{ null; }};
}};
"""
    zone_name = domain(topo)
    local = f'zone "{zone_name}" {{ type primary; file "/etc/bind/db.{zone_name}"; allow-update {{ none; }}; }};\n'
    sub = topo["dns"]["gslb_subdomain"]
    rr = ["$TTL 60",
          f"@\tIN SOA ns1.{zone_name}. hostmaster.{zone_name}. ( {int(time.time())} 3600 600 86400 60 )",
          "@\tIN NS ns1",
          f"ns1\tIN A {dns_node['ip']}",
          f"desktop\tIN A {topo['desktop']['lab_ip']}"]
    rr += [f"{n['name']}\tIN A {n['ip']}" for n in inventory(topo) if n["name"] != topo["dns"]["server"]]
    # GSLB: beide FortiADC's zijn authoritative. Valt een datacenter (en zijn FortiADC)
    # weg, dan vraagt de resolver het aan de andere.
    rr += ["", f"; GSLB: {gslb_domain(topo)} wordt beantwoord door de FortiADC's van beide datacenters"]
    for i, a in enumerate(topo["adcs"]):
        ip = next(p["ip"].split("/")[0] for p in a["ports"] if p["segment"] == "client")
        rr += [f"{sub}\tIN NS ns{i + 1}.{sub}", f"ns{i + 1}.{sub}\tIN A {ip}\t; {a['hostname']}"]
    rr.append("")
    for vs in topo["virtual_servers"]:
        rr.append(f"; {vs['title']}")
        for hname in vs["hosts"]:
            if vs.get("gslb"):
                rr.append(f"{hname}\tIN CNAME {hname}.{sub}")
            else:
                rr.append(f"{hname}\tIN A {next(iter(vs['sites'].values()))}")
        # directe namen per datacenter, handig om één DC te testen
        for site, vip in vs["sites"].items():
            rr.append(f"vip-{vs['id']}-{site.lower()}\tIN A {vip}")
    return {"/etc/bind/named.conf.options": options, "/etc/bind/named.conf.local": local,
            f"/etc/bind/db.{zone_name}": "\n".join(rr) + "\n"}


def _write_files(vmid, files: dict[str, str], mode="644"):
    h = host()
    for path, text in files.items():
        h.pct_exec(vmid, f"install -d {shlex.quote(str(Path(path).parent))} && "
                         f"printf %s {shlex.quote(text)} > {shlex.quote(path)} && chmod {mode} {shlex.quote(path)}",
                   check=True)


def setup_dns(log, topo, node):
    h = host()
    _write_files(node["vmid"], bind_config(topo))
    h.pct_exec(node["vmid"], "install -d -o bind -g bind /var/log/named && named-checkconf && "
                             f"named-checkzone {domain(topo)} /etc/bind/db.{domain(topo)} && "
                             "systemctl enable named && systemctl restart named", check=True)
    log(f"  BIND: zone {domain(topo)} met delegatie {gslb_domain(topo)} → FortiADC")


# --------------------------------------------------------------------------
# containers
# --------------------------------------------------------------------------
def _net0(topo, node) -> str:
    seg = segment_map(topo)[node["segment"]]
    return (f"name=eth0,bridge={seg['bridge']},hwaddr={node_mac(node['ip'])},"
            f"ip={node['ip']}/{prefixlen(seg['subnet'])},gw={seg['gateway']}")


def _dns_ip(topo) -> str:
    return next(n["ip"] for n in inventory(topo) if n["name"] == topo["dns"]["server"])


def push_ca(node):
    """Lab-CA in de systeem-truststore (curl, Python en de client vertrouwen de VIP's)."""
    h = host()
    _write_files(node["vmid"], {CA_PATH: pki.ca_pem()})
    h.pct_exec(node["vmid"], "update-ca-certificates >/dev/null", check=True)


def labapp_config(topo, node) -> dict:
    dc = next((d["id"] for d in datacenter_map(topo).values() if d["segment"] == node["segment"]), "-")
    app = topo["apps"].get(node.get("app"), {})
    return {"name": node["name"], "app": node.get("app"), "title": app.get("title", node["name"]),
            "color": app.get("color", "#444"), "dc": dc, "ip": node["ip"]}


def setup_role(log, topo, node):
    h = host()
    vmid = node["vmid"]
    role = node["role"]
    if role == "webapp":
        _write_files(vmid, {"/etc/labapp.json": json.dumps(labapp_config(topo, node), indent=2) + "\n"})
        h.pct_exec(vmid, "systemctl enable labapp && systemctl restart labapp", check=True)
    elif role == "dns":
        setup_dns(log, topo, node)
    elif role in ("juiceshop", "dvwa"):
        setup_docker_app(log, topo, node)


DOCKER_PORT = {"juiceshop": 3000, "dvwa": 80}


def setup_docker_app(log, topo, node):
    """De image wordt één keer opgehaald: de container hangt daarvoor kort aan de
    uplink (net als het template), daarna weer aan zijn datacenter-segment."""
    h = host()
    vmid = node["vmid"]
    image = topo["images"][node["role"]]
    h.pct_exec(vmid, "systemctl enable --now containerd docker.socket docker", check=True)
    rc, _ = h.pct_exec(vmid, f"docker image inspect {shlex.quote(image)} >/dev/null 2>&1")
    if rc != 0:
        log(f"  {node['hostname']}: image {image} ophalen (tijdelijk via de uplink)")
        tnet = topo["lab"]["template_net"]
        h.run(["pct", "stop", str(vmid)])
        h.run(["pct", "set", str(vmid), "--net0",
               f"name=eth0,bridge={topo['lab']['uplink_bridge']}{uplink_tag(topo)},{tnet}"])
        h.run(["pct", "set", str(vmid), "--delete", "nameserver,searchdomain"])  # DNS van de uplink
        h.run(["pct", "start", str(vmid)])
        try:
            wait_for_internet(log, h, vmid)
            h.pct_exec(vmid, f"docker pull -q {shlex.quote(image)}", timeout=1800, check=True)
        finally:
            h.run(["pct", "stop", str(vmid)])
            h.run(["pct", "set", str(vmid), "--net0", _net0(topo, node), "--nameserver", _dns_ip(topo),
                   "--searchdomain", domain(topo)])
            h.run(["pct", "start", str(vmid)])
            time.sleep(3)
    h.pct_exec(vmid, "docker rm -f app >/dev/null 2>&1; "
                     f"docker run -d --name app --restart unless-stopped -p 80:{DOCKER_PORT[node['role']]} "
                     f"{shlex.quote(image)}", timeout=300, check=True)
    log(f"  {node['hostname']}: {image} draait op poort 80")
    if node["role"] == "dvwa":
        rc, out = h.pct_exec(vmid, DVWA_SETUP, timeout=180)
        log(f"  {node['hostname']}: DVWA-database {'aangemaakt' if rc == 0 else 'NIET aangemaakt: ' + out[-200:]}")


# DVWA: database aanmaken via setup.php (anders begint elke demo met een setup-scherm).
DVWA_SETUP = r"""set -e
for i in $(seq 60); do curl -sf -o /dev/null http://127.0.0.1/login.php && break; sleep 2; done
c=$(mktemp)
tok=$(curl -s -c $c -b $c http://127.0.0.1/setup.php | sed -n "s/.*name='user_token' value='\([0-9a-f]*\)'.*/\1/p" | head -1)
curl -s -o /dev/null -c $c -b $c --data "create_db=Create+%2F+Reset+Database&user_token=$tok" http://127.0.0.1/setup.php
rm -f $c
"""


def deploy_node(log, topo, node):
    h = host()
    vmid = node["vmid"]
    seg = segment_map(topo)[node["segment"]]
    res = topo["resources"].get(node["role"], {"cores": 1, "memory": 256})
    if h.exists(vmid):
        log(f"{node['hostname']} ({vmid}) bestaat al")
        return
    log(f"{node['hostname']} ({vmid}) → {seg['name']} {node['ip']} [{node['role']}]")
    tpl = template_vmid(topo)
    try:
        h.run(["pct", "clone", str(tpl), str(vmid), "--hostname", node["hostname"]])
    except HostError:
        log("  linked clone niet mogelijk op deze storage, full clone")
        h.run(["pct", "clone", str(tpl), str(vmid), "--hostname", node["hostname"], "--full", "1"])
    h.run(["pct", "set", str(vmid), "--net0", _net0(topo, node),
           "--cores", str(res["cores"]), "--memory", str(res["memory"]),
           "--nameserver", _dns_ip(topo), "--searchdomain", domain(topo), "--onboot", "1",
           "--tags", f"adclab;{node['segment']};{node['role']}",
           "--description", f"adclab {node['role']} {node.get('app') or ''}".strip()])
    h.run(["pct", "start", str(vmid)])
    time.sleep(2)
    h.pct_exec(vmid, "systemd-machine-id-setup >/dev/null", check=True)
    push_ca(node)
    setup_role(log, topo, node)


def update_apps(log):
    """Nieuwe labapp-code (na een update van de controller) naar alle draaiende containers."""
    topo = load_topology()
    pki.ensure_ca(topo)
    for node in inventory(topo):
        if host().status(node["vmid"]) != "running":
            continue
        log(f"labapp bijwerken op {node['hostname']}")
        push_labapp(log, node["vmid"])
        push_ca(node)
        if node["role"] in ("webapp", "dns"):
            setup_role(log, topo, node)
    update_state(labapp_tree=updates.labapp_tree())


# --------------------------------------------------------------------------
# Ubuntu desktop
# --------------------------------------------------------------------------
def ubuntu_image(log, topo) -> Path:
    """Ubuntu cloud image downloaden en verifiëren tegen SHA256SUMS (gecachet)."""
    d = topo["desktop"]
    name = d["image_url"].rsplit("/", 1)[1]
    dest = UPLOADS / name
    sums = requests.get(d["sums_url"], timeout=30)
    sums.raise_for_status()
    want = next((l.split()[0] for l in sums.text.splitlines() if l.strip().endswith(name)), None)
    if not want:
        raise RuntimeError(f"{name} staat niet in {d['sums_url']}")

    def sha(p):
        hsh = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                hsh.update(chunk)
        return hsh.hexdigest()
    if dest.exists() and sha(dest) == want:
        return dest
    log(f"Ubuntu cloud image downloaden ({name})...")
    with requests.get(d["image_url"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest.with_suffix(".part"), "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    dest.with_suffix(".part").replace(dest)
    if sha(dest) != want:
        dest.unlink()
        raise RuntimeError("SHA256 van het Ubuntu-image klopt niet; download afgebroken")
    log("  SHA256 geverifieerd")
    return dest


def _sha512_crypt(password: str) -> str:
    return subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password, capture_output=True,
                          text=True, check=True).stdout.strip()


def desktop_bookmarks(topo) -> list[dict]:
    marks = []
    for vs in topo["virtual_servers"]:
        for hname in vs["hosts"]:
            marks.append({"name": topo["apps"].get(hname, {}).get("title", hname),
                          "url": f"https://{hname}.{domain(topo)}/"})
    marks.append({"name": "Portal → /api/orders (content routing)", "url": f"https://portal.{domain(topo)}/api/orders"})
    for a in topo["adcs"]:
        try:
            marks.append({"name": f"FortiADC {a['dc']}", "url": fortiadc.base_url(topo, a["site"]) + "/"})
        except RuntimeError:
            pass
    return marks


def firefox_policies(topo) -> dict:
    marks = desktop_bookmarks(topo)
    locked = lambda v: {"Value": v, "Status": "locked"}  # noqa: E731
    return {"policies": {
        "Certificates": {"ImportEnterpriseRoots": True, "Install": [CA_PATH]},
        "DNSOverHTTPS": {"Enabled": False, "Locked": True},  # anders omzeilt Firefox de lab-DNS
        "Preferences": {
            # GSLB-demo: geen DNS-cache in de browser, elke navigatie vraagt opnieuw.
            "network.dnsCacheExpiration": locked(0),
            "network.dnsCacheExpirationGracePeriod": locked(0),
            "browser.aboutConfig.showWarning": locked(False),
        },
        "Homepage": {"URL": marks[0]["url"], "StartPage": "homepage"},
        "DisplayBookmarksToolbar": "always",
        "ManagedBookmarks": [{"toplevel_name": "FortiADC Lab"}] + marks,
        "DisableTelemetry": True, "DisableFirefoxStudies": True, "DisablePocket": True,
        "DontCheckDefaultBrowser": True, "OverrideFirstRunPage": "", "OverridePostUpdatePage": "",
        "UserMessaging": {"WhatsNew": False, "ExtensionRecommendations": False, "FeatureRecommendations": False,
                          "UrlbarInterventions": False, "SkipOnboarding": True, "MoreFromMozilla": False},
        "PasswordManagerEnabled": False,
    }}


DESKTOP_RUNCMD = r"""set -eux
export DEBIAN_FRONTEND=noninteractive
update-ca-certificates
# Firefox als .deb uit Mozilla's eigen repository (officiële instructie): de snap
# leest geen enterprise policies/CA's uit /etc. Sleutel controleren op fingerprint.
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg -o /etc/apt/keyrings/packages.mozilla.org.asc
gpg -n -q --import --import-options import-show /etc/apt/keyrings/packages.mozilla.org.asc \
  | grep -q 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3
echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
  > /etc/apt/sources.list.d/mozilla.list
printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
apt-get update -q
apt-get install -yq -o Dpkg::Options::=--force-confold ubuntu-desktop-minimal firefox \
  qemu-guest-agent spice-vdagent dnsutils curl openssl xrdp
snap remove --purge firefox || true
systemctl enable --now qemu-guest-agent
# Automatisch inloggen op de console; geen schermvergrendeling tijdens een demo.
printf '[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=__USER__\n' > /etc/gdm3/custom.conf
install -d /etc/dconf/profile /etc/dconf/db/local.d
printf 'user-db:user\nsystem-db:local\n' > /etc/dconf/profile/user
printf "[org/gnome/desktop/session]\nidle-delay=uint32 0\n[org/gnome/desktop/screensaver]\nlock-enabled=false\n[org/gnome/shell]\nfavorite-apps=['firefox.desktop', 'org.gnome.Terminal.desktop', 'org.gnome.Nautilus.desktop']\n" \
  > /etc/dconf/db/local.d/00-adclab
dconf update
adduser xrdp ssl-cert
systemctl enable xrdp
"""


def desktop_macs(topo) -> tuple[str, str]:
    """Vaste MAC's (uplink, lab), afgeleid van het VMID: uniek per lab op hetzelfde netwerk."""
    vmid = topo["desktop"]["vmid"]
    return (node_mac(f"0.{vmid >> 16 & 255}.{vmid >> 8 & 255}.{vmid & 255}", "02:AD:D0"),
            node_mac(topo["desktop"]["lab_ip"], "02:AD:D1"))


def desktop_seed(topo) -> Path:
    """NoCloud seed-ISO (cidata) voor de Ubuntu desktop."""
    d = topo["desktop"]
    segs = segment_map(topo)
    client = segs["client"]
    user = d["user"]
    pw = generated_secret("desktop_password")
    uplink = get_state().get("desktop_uplink") or {"mode": "dhcp"}
    up_mac, lab_mac = desktop_macs(topo)
    if uplink["mode"] == "static":
        up_cfg = {"addresses": [uplink["ip"]], "routes": [{"to": "default", "via": uplink["gateway"]}],
                  "nameservers": {"addresses": [x for x in (uplink.get("dns"),) if x]}}
    else:
        up_cfg = {"dhcp4": True}
    network = {"version": 2, "ethernets": {
        "uplink": {"match": {"macaddress": up_mac.lower()}, "set-name": "uplink", **up_cfg},
        # Lab-NIC: de VIP's zitten in dit subnet. Alleen het lab-domein gaat naar de lab-DNS
        # (systemd-resolved, split DNS); internet-namen via de uplink.
        "lab": {"match": {"macaddress": lab_mac.lower()}, "set-name": "lab",
                "addresses": [f"{d['lab_ip']}/{prefixlen(client['subnet'])}"],
                "nameservers": {"addresses": [_dns_ip(topo)], "search": [domain(topo)]}},
    }}
    user_data = {
        "hostname": vm_name(topo, d["name"]),
        "fqdn": f"{d['name']}.{domain(topo)}",
        "timezone": topo["lab"].get("timezone", "Europe/Amsterdam"),
        "keyboard": {"layout": "us"},
        "users": [{"name": user, "gecos": "FortiADC Lab demo", "groups": ["sudo", "adm"], "shell": "/bin/bash",
                   "lock_passwd": False, "passwd": _sha512_crypt(pw)}],
        "ssh_pwauth": False,
        "package_update": True,
        "package_upgrade": True,
        "write_files": [
            {"path": CA_PATH, "permissions": "0644", "content": pki.ca_pem()},
            {"path": "/etc/firefox/policies/policies.json", "permissions": "0644",
             "content": json.dumps(firefox_policies(topo), indent=2)},
            # Resolver-cache uit: elke dig/browser-navigatie ziet het actuele GSLB-antwoord.
            {"path": "/etc/systemd/resolved.conf.d/adclab.conf", "permissions": "0644",
             "content": "[Resolve]\nCache=no\n"},
        ],
        "runcmd": [["bash", "-c", DESKTOP_RUNCMD.replace("__USER__", user)]],
        "power_state": {"mode": "reboot", "message": "desktop klaar, herstarten", "condition": True},
    }
    out = Path(tempfile.mkdtemp(prefix="seed-"))
    (out / "user-data").write_text("#cloud-config\n" + yaml.safe_dump(user_data, sort_keys=False, width=200))
    (out / "meta-data").write_text(f"instance-id: {vm_name(topo, d['name'])}-{int(time.time())}\n"
                                   f"local-hostname: {vm_name(topo, d['name'])}\n")
    (out / "network-config").write_text(yaml.safe_dump(network, sort_keys=False))
    iso = out / DESKTOP_ISO
    subprocess.run(["genisoimage", "-quiet", "-J", "-r", "-V", "cidata", "-o", str(iso),
                    str(out / "user-data"), str(out / "meta-data"), str(out / "network-config")], check=True)
    return iso


def deploy_desktop(log, topo):
    h = host()
    hcfg = load_host()
    d = topo["desktop"]
    vmid = d["vmid"]
    if h.exists(vmid):
        log(f"desktop VM {vmid} bestaat al, overslaan")
        return
    pki.ensure_ca(topo)
    img = ubuntu_image(log, topo)
    remote = f"{h.workdir}/{img.name}"
    _put_with_progress(log, img, remote)
    _install_iso(desktop_seed(topo), DESKTOP_ISO)
    client = segment_map(topo)["client"]
    up_mac, lab_mac = desktop_macs(topo)
    try:
        log(f"desktop VM {vmid} aanmaken")
        h.run(["qm", "create", str(vmid), "--name", vm_name(topo, d["name"]), "--ostype", "l26", "--cpu", "host",
               "--cores", str(d["cores"]), "--memory", str(d["memory"]), "--scsihw", "virtio-scsi-single",
               "--vga", "virtio", "--agent", "enabled=1", "--tags", "adclab", "--onboot", "1",
               "--net0", f"virtio={up_mac},bridge={topo['lab']['uplink_bridge']}{uplink_tag(topo)},firewall=0",
               "--net1", f"virtio={lab_mac},bridge={client['bridge']},firewall=0",
               "--description", "Ubuntu desktop (FortiADC Lab): vertrouwt de lab-CA, gebruikt de lab-DNS"], log=log)
        _import_boot_disk(log, vmid, remote, hcfg["storage"], "scsi", ",discard=on,ssd=1")
        h.run(["qm", "disk", "resize", str(vmid), "scsi0", f"{d['disk_gb']}G"], log=log)
        h.run(["qm", "set", str(vmid), "--ide2", f"local:iso/{DESKTOP_ISO},media=cdrom", "--boot", "order=scsi0"],
              log=log)
    except Exception:
        log(f"aanmaken mislukt; half-aangemaakte VM {vmid} opruimen")
        h.run(["qm", "destroy", str(vmid), "--purge", "1"], check=False)
        raise
    h.run(["qm", "start", str(vmid)], log=log)
    log("desktop installeert zichzelf via cloud-init (Ubuntu Desktop + Firefox): reken op 15-30 minuten. "
        "Daarna herstart hij en logt automatisch in.")


def desktop_progress() -> str:
    """cloud-init-status van de desktop (via de qemu-guest-agent)."""
    topo = load_topology()
    vmid = topo["desktop"]["vmid"]
    h = host()
    if not h.exists(vmid):
        return "niet uitgerold"
    out = h.run(["qm", "guest", "exec", str(vmid), "--", "cloud-init", "status"], check=False, timeout=30)
    try:
        data = json.loads(out)
        return (data.get("out-data") or data.get("err-data") or "").strip() or "onbekend"
    except ValueError:
        return "guest agent nog niet actief (installatie loopt nog)"


# --------------------------------------------------------------------------
# orchestratie
# --------------------------------------------------------------------------
def deploy_all(log):
    topo = load_topology()
    pki.ensure_ca(topo)
    log("== 1/6 netwerksegmenten ==")
    ensure_bridges(log, topo)
    log("== 2/6 FortiADC's (één per datacenter) ==")
    for a in topo["adcs"]:
        deploy_fortiadc(log, topo, a["site"])
    log("== 3/6 LXC-template ==")
    build_template(log, topo)
    log("== 4/6 realservers, DNS en client ==")
    for node in inventory(topo):
        deploy_node(log, topo, node)
    log("== 5/6 Ubuntu desktop ==")
    deploy_desktop(log, topo)
    log("== 6/6 FortiADC-configuratie (SLB en WAF per datacenter, GSLB op beide) ==")
    fortiadc.provision(log, topo)
    update_state(deployed=True, labapp_tree=updates.labapp_tree())


def component_ids(topo) -> list[str]:
    return (["network"] + [f"fortiadc:{a['site']}" for a in topo["adcs"]] + ["template"]
            + [f"node:{n['name']}" for n in inventory(topo)]
            + ["desktop", "config"])


def _destroy_vmid(log, h, vmid, label):
    kind = h.exists(vmid)
    if not kind:
        log(f"{label} ({vmid}) bestaat niet, overslaan")
        return
    tool = "qm" if kind == "qemu" else "pct"
    log(f"{label} ({vmid}) verwijderen")
    h.run([tool, "stop", str(vmid)], check=False)
    h.run([tool, "destroy", str(vmid), "--purge", "1"], log=log)


def destroy_selected(log, items):
    topo = load_topology()
    h = host()
    items = set(items)
    for n in [n for n in inventory(topo) if f"node:{n['name']}" in items]:
        _destroy_vmid(log, h, n["vmid"], n["hostname"])
    if "desktop" in items:
        _destroy_vmid(log, h, topo["desktop"]["vmid"], vm_name(topo, topo["desktop"]["name"]))
    for a in topo["adcs"]:
        if f"fortiadc:{a['site']}" in items:
            _destroy_vmid(log, h, a["vmid"], a["hostname"])
            update_adc_state(a["site"], provisioned=False, info=None, problems=[])
    if "template" in items:
        try:
            _destroy_vmid(log, h, template_vmid(topo), vm_name(topo, "template"))
        except HostError as e:
            raise RuntimeError(f"template kan niet weg: {e}. Linked clones gebruiken het nog.") from None
    if "network" in items:
        ids = [a["vmid"] for a in topo["adcs"]] + [topo["desktop"]["vmid"], template_vmid(topo)] + \
              [n["vmid"] for n in inventory(topo)]
        still = [v for v in ids if h.exists(v)]
        if still:
            log(f"bridges niet verwijderd: nog {len(still)} lab-VM's/containers gebruiken ze")
        else:
            remove_bridges(log, topo)


def deploy_selected(log, items, recreate=False):
    topo = load_topology()
    pki.ensure_ca(topo)
    items = set(items)
    nodes = [n for n in inventory(topo) if f"node:{n['name']}" in items]
    if recreate:
        log("== gekozen componenten eerst verwijderen ==")
        destroy_selected(log, items - {"network", "config"})
    sites = [a["site"] for a in topo["adcs"] if f"fortiadc:{a['site']}" in items]
    if sites or items & {"network", "desktop"} or nodes:
        log("== netwerksegmenten ==")
        ensure_bridges(log, topo)
    for site in sites:
        log(f"== FortiADC {site} ==")
        deploy_fortiadc(log, topo, site)
    if "template" in items or nodes:
        log("== LXC-template ==")
        build_template(log, topo)
    for node in nodes:
        deploy_node(log, topo, node)
    if "desktop" in items:
        log("== Ubuntu desktop ==")
        deploy_desktop(log, topo)
    if sites or "config" in items:
        log("== FortiADC-configuratie ==")
        # 'config' = beide; alleen een nieuwe FortiADC: die krijgt SLB + GSLB.
        fortiadc.provision(log, topo, None if "config" in items else sites)


def destroy_all(log, remove_template=False, remove_net=False):
    topo = load_topology()
    h = host()
    ids = [n["vmid"] for n in inventory(topo)] + [topo["desktop"]["vmid"]] + [a["vmid"] for a in topo["adcs"]]
    if remove_template:
        ids.append(template_vmid(topo))
    for vmid in ids:
        kind = h.exists(vmid)
        if not kind:
            continue
        tool = "qm" if kind == "qemu" else "pct"
        log(f"{tool} destroy {vmid}")
        h.run([tool, "stop", str(vmid)], check=False)
        h.run([tool, "destroy", str(vmid), "--purge", "1"], check=False)
    if remove_net:
        remove_bridges(log, topo)
    update_state(deployed=False, adc={})
