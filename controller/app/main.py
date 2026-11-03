"""Web UI en API van de FortiADC Lab controller."""
import asyncio
import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Optional

import markdown
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import adcconfig, auth, demo, deployer, fortiadc, jobs, pki, updates
from .config import (DATA, HOST_FILE, ROOT, UPLOADS, domain, generated_secret, get_state, gslb_domain, inventory,
                     load_host, load_topology, template_vmid, update_state)
from .hostctl import host

HERE = Path(__file__).parent
DOCS = ROOT / "docs" / "demo"

app = FastAPI(title="FortiADC Lab", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

PUBLIC_PATHS = ("/login", "/static/", "/ca.crt")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PATHS) or auth.valid_session(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "niet ingelogd"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


STATIC_REF = re.compile(r'(/static/[\w.-]+\.(?:css|js))"')
NO_CACHE = {"Cache-Control": "no-cache"}


def _page(name: str) -> str:
    """HTML-template met ?v=<hash> achter de static-bestanden (geen oude app.js na een update)."""
    def versioned(m):
        digest = hashlib.sha1((HERE / m.group(1).lstrip("/")).read_bytes()).hexdigest()[:10]
        return f'{m.group(1)}?v={digest}"'
    return STATIC_REF.sub(versioned, (HERE / "templates" / name).read_text())


def _login_page(error: str = "") -> HTMLResponse:
    block = f'<div class="login-error">{error}</div>' if error else ""
    return HTMLResponse(_page("login.html").replace("<!--ERROR-->", block), status_code=401 if error else 200,
                        headers=NO_CACHE)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if auth.valid_session(request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/", status_code=303)
    return _login_page()


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    if not auth.check_credentials(username, password):
        await asyncio.sleep(1)  # brute force afremmen
        return _login_page("Onjuiste gebruikersnaam of wachtwoord.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.new_session(), max_age=auth.SESSION_SECONDS,
                    httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_page("index.html"), headers=NO_CACHE)


@app.get("/ca.crt")
def ca_cert():
    """De lab-root-CA (publiek: het is een certificaat, geen sleutel), om ook op een
    presentatielaptop te vertrouwen. Hij is beperkt tot het lab-domein (Name Constraints)."""
    topo = load_topology()
    pki.ensure_ca(topo)
    return Response(pki.ca_pem(), media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="fortiadc-lab-root-ca.crt"'})


# --- status & instellingen -----------------------------------------------
# Dit is een lab: wachtwoorden gaan bewust leesbaar naar de UI (achter de login).
def public_state() -> dict:
    s = get_state()
    topo = load_topology()
    wan = topo["fortiadc"]["wan"]
    adcs = topo["adcs"]
    images = {}
    for kind in deployer.ADC_IMAGES:
        name = s.get(f"adc_{kind}_image")
        images[kind] = {"name": s.get(f"adc_{kind}_upload") or name,
                        "size": (UPLOADS / name).stat().st_size if name and (UPLOADS / name).exists() else None}
    lic_mode = s.get("license_mode", "none")
    checks = [
        {"id": "host", "label": "Proxmox-koppeling", "ok": HOST_FILE.exists()},
        {"id": "image", "label": "FortiADC-images", "ok": all(i["size"] for i in images.values())},
        {"id": "wan", "label": "Management port1",
         "ok": wan["mode"] == "dhcp" or bool(wan.get("gateway") and all(a["mgmt"] for a in adcs))},
        {"id": "admin", "label": "Admin-wachtwoord", "ok": bool(s.get("adc_admin_password"))},
        {"id": "license", "label": "Licentie", "optional": True,
         "ok": all((lic_mode == "file" and (s.get("license_files") or {}).get(a["site"]))
                   or (lic_mode == "fortiflex" and (s.get("fortiflex_tokens") or {}).get(a["site"])) for a in adcs)},
    ]
    return {
        "wan": wan, "adc_images": images, "license_mode": lic_mode,
        "adcs": [{"site": a["site"], "dc": a["dc"], "name": a["hostname"], "mgmt": a["mgmt"],
                  "mgmt_dhcp": a["mgmt_dhcp"], "license_file": (s.get("license_files") or {}).get(a["site"]),
                  "fortiflex_token": (s.get("fortiflex_tokens") or {}).get(a["site"]) or ""} for a in adcs],
        "adc_admin_password": s.get("adc_admin_password") or "",
        "desktop_uplink": s.get("desktop_uplink") or {"mode": "dhcp"},
        "dns_suggestion": _resolvers(), "deployed": s.get("deployed", False),
        "provisioned": s.get("adc_provisioned", False), "checks": checks,
    }


def _resolvers() -> list[str]:
    try:
        lines = Path("/etc/resolv.conf").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            try:
                if ipaddress.ip_address(parts[1]).version == 4 and not ipaddress.ip_address(parts[1]).is_loopback:
                    out.append(parts[1])
            except ValueError:
                continue
    return out[:2]


@app.get("/api/settings")
def get_settings():
    return public_state()


@app.get("/api/host")
def host_info():
    if not HOST_FILE.exists():
        return {"configured": False}
    h = load_host()
    topo = load_topology()
    base = topo["lab"]["vmid_base"]
    pw_file = DATA / "ui_password"
    return {
        "configured": True, "host": h["host"], "node": h["node"], "ssh_user": h.get("ssh_user", "root"),
        "sudo": h.get("ssh_user", "root") != "root", "workdir": h.get("workdir", "/var/lib/vz/adclab"),
        "storage": h["storage"], "template": h["template"], "controller_ctid": h.get("controller_ctid"),
        "uplink_bridge": topo["lab"]["uplink_bridge"], "uplink_vlan": topo["lab"].get("uplink_vlan") or "",
        "template_net": topo["lab"]["template_net"], "vmid_range": f"{base}-{base + 99}",
        "name_prefix": topo["lab"]["name_prefix"],
        "adc_vmids": " · ".join(f"{a['hostname']} {a['vmid']}" for a in topo["adcs"]),
        "template_vmid": template_vmid(topo), "desktop_vmid": topo["desktop"]["vmid"],
        "ui_password": pw_file.read_text().strip() if pw_file.exists() else "",
    }


@app.post("/api/host/test")
def host_test():
    try:
        h = host()
        who = h.run("id -un").strip()
        v = json.loads(h.run(["pvesh", "get", "/version", "--output-format", "json"]))
        return {"ok": True, "detail": f"ingelogd als {who} op {h.node}; Proxmox VE {v.get('version')} "
                                      f"({'via sudo' if h.sudo else 'als root'})"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)}


@app.get("/api/status")
def status():
    try:
        lab, err = deployer.lab_status(), None
    except Exception as e:  # noqa: BLE001 - host onbereikbaar: topologie tonen met onbekende status
        topo = load_topology()
        lab = {"adcs": [{"site": a["site"], "dc": a["dc"], "vmid": a["vmid"], "status": "unknown", "name": a["hostname"],
                         "mgmt": a["mgmt"], "ports": a["ports"], "info": {}} for a in topo["adcs"]],
               "template": {"vmid": template_vmid(topo), "status": "unknown"},
               "desktop": {"vmid": topo["desktop"]["vmid"], "status": "unknown", "ip": topo["desktop"]["lab_ip"],
                           "name": f"{topo['lab']['name_prefix']}-{topo['desktop']['name']}"},
               "segments": [{**s, "present": False} for s in topo["segments"]],
               "nodes": [{**n, "status": "unknown"} for n in inventory(topo)]}
        err = str(e)
    return {"settings": public_state(), "lab": lab, "host_error": err, "jobs": jobs.recent()}


class Settings(BaseModel):
    wan_mode: Optional[str] = None
    wan_gateway: Optional[str] = None
    wan_dns1: Optional[str] = None
    wan_dns2: Optional[str] = None
    adc_ips: Optional[dict[str, str]] = None        # per datacenter, statisch: IP met prefix
    adc_mgmt_ip: Optional[dict[str, str]] = None    # per datacenter, bij DHCP: het gekregen IP
    admin_password: Optional[str] = None
    license_mode: Optional[str] = None
    fortiflex_tokens: Optional[dict[str, str]] = None
    desktop_mode: Optional[str] = None
    desktop_ip: Optional[str] = None
    desktop_gateway: Optional[str] = None
    desktop_dns: Optional[str] = None


def _ipv4(value: str, what: str) -> str:
    try:
        if ipaddress.ip_address(value).version != 4:
            raise ValueError
    except ValueError:
        raise HTTPException(400, f"ongeldig {what}: {value} (IPv4-adres verwacht)")
    return value


def _iface(value: str, what: str):
    try:
        iface = ipaddress.ip_interface(value)
        if "/" not in value or iface.network.prefixlen == 32:
            raise ValueError
    except ValueError:
        raise HTTPException(400, f"{what}: geef IP met prefix, bijv. 192.168.1.98/24")
    return iface


@app.post("/api/settings")
def save_settings(s: Settings):
    changes = {}
    sites = [a["site"] for a in load_topology()["adcs"]]
    if s.wan_mode:
        if s.wan_mode not in ("static", "dhcp"):
            raise HTTPException(400, "wan_mode moet static of dhcp zijn")
        gw = (s.wan_gateway or "").strip()
        ips = {k: v.strip() for k, v in (s.adc_ips or {}).items() if k in sites}
        if s.wan_mode == "static":
            if not gw:
                raise HTTPException(400, "gateway is verplicht: zonder default route geen FortiGuard/licentie")
            seen = set()
            for site in sites:
                iface = _iface(ips.get(site, ""), f"Management-IP {site}")
                if ipaddress.ip_address(_ipv4(gw, "gateway")) not in iface.network:
                    raise HTTPException(400, f"gateway {gw} ligt niet in {iface.network} ({site})")
                if iface.ip in seen:
                    raise HTTPException(400, "de FortiADC's hebben elk een eigen management-IP nodig")
                seen.add(iface.ip)
            changes["adc_ips"] = ips
        dns = [_ipv4(d.strip(), "DNS-server") if (d or "").strip() else "" for d in (s.wan_dns1, s.wan_dns2)]
        changes["wan"] = {"mode": s.wan_mode, "gateway": gw if s.wan_mode == "static" else "",
                          "dns1": dns[0], "dns2": dns[1]}
    if s.adc_mgmt_ip is not None:
        changes["adc_mgmt_ip"] = {k: _ipv4(v.strip(), f"management-IP {k}") if v.strip() else ""
                                  for k, v in s.adc_mgmt_ip.items() if k in sites}
    if s.admin_password:
        if len(s.admin_password) < 8 or re.search(r"[\"'\s\\]", s.admin_password):
            raise HTTPException(400, "admin-wachtwoord: minimaal 8 tekens, geen spaties, quotes of backslash")
        changes["adc_admin_password"] = s.admin_password
    if s.license_mode:
        if s.license_mode not in ("none", "file", "fortiflex"):
            raise HTTPException(400, "ongeldige licentiemodus")
        changes["license_mode"] = s.license_mode
    if s.fortiflex_tokens:
        tokens = dict(get_state().get("fortiflex_tokens") or {})
        for site, tok in s.fortiflex_tokens.items():
            tok = (tok or "").strip()
            if site not in sites or not tok:
                continue
            if not re.fullmatch(r"[A-Za-z0-9]{8,64}", tok):
                raise HTTPException(400, f"FortiFlex-token van {site} heeft een onverwacht formaat")
            tokens[site] = tok
        changes["fortiflex_tokens"] = tokens
    if s.desktop_mode:
        if s.desktop_mode == "static":
            iface = _iface((s.desktop_ip or "").strip(), "Desktop-IP")
            gw = _ipv4((s.desktop_gateway or "").strip(), "gateway")
            if ipaddress.ip_address(gw) not in iface.network:
                raise HTTPException(400, f"gateway {gw} ligt niet in {iface.network}")
            dns = _ipv4(s.desktop_dns.strip(), "DNS-server") if (s.desktop_dns or "").strip() else ""
            changes["desktop_uplink"] = {"mode": "static", "ip": str(iface), "gateway": gw, "dns": dns}
        else:
            changes["desktop_uplink"] = {"mode": "dhcp"}
    update_state(**changes)
    return public_state()


async def _stream_upload(request: Request, filename: str, allowed: tuple[str, ...]) -> str:
    name = Path(filename).name
    if not name.lower().endswith(allowed) or name.startswith("."):
        raise HTTPException(400, f"verwacht bestand met extensie {', '.join(allowed)}")
    with open(UPLOADS / name, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return name


@app.post("/api/upload/image")
async def upload_image(request: Request, filename: str, kind: str):
    """boot.qcow2 of data.qcow2 uit de FortiADC KVM-zip; opgeslagen onder een vaste naam."""
    if kind not in deployer.ADC_IMAGES:
        raise HTTPException(400, "kind moet boot of data zijn")
    if not filename.lower().endswith(".qcow2"):
        raise HTTPException(400, "verwacht een .qcow2-bestand")
    target = deployer.ADC_IMAGES[kind]
    with open(UPLOADS / target, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    update_state(**{f"adc_{kind}_image": target, f"adc_{kind}_upload": Path(filename).name})
    return public_state()


@app.post("/api/upload/license")
async def upload_license(request: Request, filename: str, site: str):
    if site not in [a["site"] for a in load_topology()["adcs"]]:
        raise HTTPException(400, "onbekend datacenter")
    files = dict(get_state().get("license_files") or {})
    files[site] = await _stream_upload(request, filename, (".lic",))
    update_state(license_files=files, license_mode="file")
    return public_state()


# --- updates -----------------------------------------------------------------
@app.get("/api/version")
def version():
    return updates.version()


@app.post("/api/update/check")
def update_check():
    try:
        return updates.check()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"controleren mislukt: {e}")


@app.post("/api/update/run")
def update_run():
    try:
        updates.start()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, str(e))
    return {"started": True}


@app.get("/api/update/log")
def update_log(offset: int = 0):
    return updates.log_lines(offset)


# --- lab-acties ------------------------------------------------------------
def _start(title, fn, *args, exclusive=True):
    try:
        return jobs.start(title, fn, *args, exclusive=exclusive)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/deploy")
def deploy():
    return _start("Volledig lab uitrollen", deployer.deploy_all)


@app.post("/api/provision")
def provision():
    return _start("FortiADC-configuratie toepassen (beide)", lambda log: fortiadc.provision(log, load_topology()))


@app.post("/api/update-apps")
def update_apps():
    return _start("Labapp bijwerken", deployer.update_apps)


class Destroy(BaseModel):
    template: bool = False
    network: bool = False


class Selection(BaseModel):
    items: list[str]
    recreate: bool = False


def _check_items(items: list[str]) -> list[str]:
    allowed = set(deployer.component_ids(load_topology()))
    bad = [i for i in items if i not in allowed]
    if bad or not items:
        raise HTTPException(400, f"onbekende of geen componenten: {', '.join(bad) or '-'}")
    return items


@app.post("/api/deploy/selected")
def deploy_selected(sel: Selection):
    items = _check_items(sel.items)
    title = ("Opnieuw uitrollen" if sel.recreate else "Uitrollen") + f" ({len(items)} componenten)"
    return _start(title, deployer.deploy_selected, items, sel.recreate)


@app.post("/api/destroy/selected")
def destroy_selected(sel: Selection):
    items = _check_items(sel.items)
    return _start(f"Verwijderen ({len(items)} componenten)", deployer.destroy_selected, items)


@app.post("/api/destroy")
def destroy(d: Destroy):
    return _start("Lab afbreken", deployer.destroy_all, d.template, d.network)


@app.get("/api/jobs/{job_id}")
def job(job_id: str, offset: int = 0):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404)
    return {**{k: v for k, v in j.items() if k != "lines"}, "lines": j["lines"][offset:], "offset": len(j["lines"])}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    if not jobs.stop(job_id):
        raise HTTPException(404, "geen lopende taak met dit id")
    return {"ok": True}


# --- FortiADC-config ------------------------------------------------------------
@app.get("/api/config", response_class=PlainTextResponse)
def adc_config():
    return PlainTextResponse(adcconfig.render(load_topology()))


@app.get("/api/problems")
def problems():
    return {"problems": fortiadc.problems_all(load_topology())}


@app.get("/api/desktop/progress")
def desktop_progress():
    try:
        return {"status": deployer.desktop_progress()}
    except Exception as e:  # noqa: BLE001
        return {"status": f"onbekend ({e})"}


# --- demo-cockpit ------------------------------------------------------------
@app.get("/api/demo/catalog")
def demo_catalog():
    topo = load_topology()
    return {"catalog": demo.CATALOG, "servers": [{"name": n["name"], "app": n.get("app"), "ip": n["ip"],
                                                   "segment": n["segment"]} for n in demo.webservers(topo)]}


@app.get("/api/demo/servers")
def demo_servers():
    try:
        return {"servers": demo.server_states()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


class DemoAction(BaseModel):
    action: str
    arg: Any = None
    label: str = ""


@app.post("/api/demo/run")
def demo_run(a: DemoAction):
    if a.action not in demo.ACTIONS or not demo.allowed(a.action, a.arg):
        raise HTTPException(400, "onbekende demo-actie")
    return _start(a.label or a.action, demo.run, a.action, a.arg, exclusive=False)


# --- toegang & demo-scripts --------------------------------------------------
@app.get("/api/access")
def access():
    topo = load_topology()
    d = topo["desktop"]
    adcs = []
    for a in topo["adcs"]:
        try:
            url = fortiadc.base_url(topo, a["site"])
        except RuntimeError:
            url = ""
        adcs.append({"site": a["site"], "dc": a["dc"], "name": a["hostname"], "url": url,
                     "dns_ip": next(p["ip"].split("/")[0] for p in a["ports"] if p["segment"] == "client")})
    fp = ""
    if pki.CA_CERT.exists():
        fp = pki.ca_fingerprint()
    return {
        "adcs": adcs, "adc_user": "admin", "adc_password": get_state().get("adc_admin_password") or "",
        "desktop_user": d["user"], "desktop_password": generated_secret("desktop_password"),
        "desktop_vmid": d["vmid"], "desktop_lab_ip": d["lab_ip"], "ca_fingerprint": fp,
        "domain": domain(topo), "gslb_domain": gslb_domain(topo),
        "urls": deployer.desktop_bookmarks(topo),
        "names": [{"host": f"{h}.{domain(topo)}", "vs": vs["id"], "gslb": bool(vs.get("gslb")),
                   "sites": vs["sites"], "title": vs["title"]} for vs in topo["virtual_servers"] for h in vs["hosts"]],
    }


@app.get("/api/docs")
def docs_list():
    out = []
    for p in sorted(DOCS.glob("*.md")):
        first = next((l[2:].strip() for l in p.read_text().splitlines() if l.startswith("# ")), p.stem)
        out.append({"name": p.name, "title": first})
    return {"docs": out}


@app.get("/api/docs/{name}", response_class=HTMLResponse)
def doc(name: str):
    p = DOCS / Path(name).name
    if not p.exists() or p.suffix != ".md":
        raise HTTPException(404)
    return HTMLResponse(markdown.markdown(p.read_text(), extensions=["tables", "fenced_code"]), headers=NO_CACHE)
