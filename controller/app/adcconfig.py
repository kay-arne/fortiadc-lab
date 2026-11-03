"""FortiADC-configuratie, gegenereerd uit lab/topology.yaml.

Elk datacenter heeft een eigen FortiADC. Per FortiADC bestaat de config uit:
- SLB: health checks, real servers, pools, persistence, content routing, WAF en de
  virtual servers van zíjn datacenter;
- GSLB: op beide FortiADC's gelijkwaardig. Beide kennen dezelfde data centers, pools
  en hosts, en beide beantwoorden DNS voor gslb.<domein>. Zo blijft GSLB werken als
  één heel datacenter (inclusief zijn FortiADC) wegvalt.

GSLB-servers (gezien vanaf de FortiADC van datacenter X):
  DEFAULT_DNS_SERVER  ingebouwd (127.0.0.1): de FortiADC zelf, datacenter X. Met
            auto-sync kent hij de eigen virtual servers en hun status (gezond als de
            pool gezonde leden heeft).
  FAD-<dc>  type FortiADC-SLB: de FortiADC van het ándere datacenter, op zijn
            management-IP (TCP 5858, auto-sync).
  GEN-<dc>  type Generic Host: voor FQDN's die één VIP delen (content routing). Elke
            FQDN is een eigen lid met een eigen HTTPS-health check mét Host-header,
            zodat hij los van de andere FQDN's op dezelfde VIP kan failoveren.

Alles is gewone FortiADC CLI (7.4/7.6); de controller past het per regel toe via SSH
en de UI toont dezelfde tekst ('Config bekijken'). Certificaten, certificaatgroepen
en client-SSL-profielen gaan via de REST API (fortiadc.upload_certificates()).

Naamgeving (best practice: type-prefix, zodat objecten in de GUI te groeperen zijn):
  HC-<app>          health check naar de realserver
  HC-GSLB-<host>    health check van GSLB naar een VIP, mét Host-header (per FQDN)
  RS-<node>         real server             POOL-<app>-<dc>   real server pool
  CR-<vs>-<dc>-<r>  content route           VS-<vs>-<dc>      virtual server
  GP-<host>[-<dc>]  GSLB virtual server pool, GH-<host> GSLB host
"""
from __future__ import annotations

from .config import adc, datacenter_map, domain, gslb_domain, inventory, pool_members
from .pki import cert_name

PERSISTENCE = {"cookie": "PERS-COOKIE", "source": "LB_PERSIS_SRC_ADDR"}
WAF_PROFILES = {
    # Blokkeren: signatures + heuristische SQLi/XSS-detectie op het hoogste niveau.
    "WAF-LAB-BLOCK": ("High-Level-Security", "Demo: blokkeert aanvallen (signatures + SQLi/XSS-heuristiek)"),
    # Monitoren: zelfde detectie, alleen loggen. Zo laat je eerst zien wát er gebeurt.
    "WAF-LAB-MONITOR": ("Alert-Only", "Demo: detecteert en logt, blokkeert niet"),
}


def up(s: str) -> str:
    return s.upper().replace("_", "-")


def vs_name(vs, site) -> str:
    return f"VS-{up(vs['id'])}-{site}"


def pool_name(app, site) -> str:
    return f"POOL-{up(app)}-{site}"


def route_name(vs, site, route) -> str:
    return f"CR-{up(vs['id'])}-{site}-{up(route['name'])}"


def hc_name(app) -> str:
    return f"HC-{up(app)}"


def gslb_hc_name(host) -> str:
    return f"HC-GSLB-{up(host)}"


def slb_server(site) -> str:
    return f"FAD-{site}"


def generic_server(site) -> str:
    return f"GEN-{site}"


def ssl_names(vs) -> tuple[str, str, str]:
    """(certificaat, local certificate group, client SSL profile)."""
    c = cert_name(vs)
    return c, f"LCG-{up(vs['id'])}", f"CSSL-{up(vs['id'])}"


def q(v) -> str:
    return f'"{v}"'


def _block(path: str, entries: list[tuple[str, list[str]]]) -> list[str]:
    """config <path> / edit <naam> / set ... / next / end (ook genest bruikbaar)."""
    lines = [f"config {path}"]
    for name, body in entries:
        lines.append(f"    edit {q(name)}")
        lines += [f"        {b}" for b in body]
        lines.append("    next")
    lines.append("end")
    return lines


def site_vses(topo, site) -> list[dict]:
    return [vs for vs in topo["virtual_servers"] if site in vs["sites"]]


def mgmt_ip(topo, site) -> str:
    a = adc(topo, site)
    return (a["mgmt"].split("/")[0] if a["mgmt"] else a["mgmt_dhcp"]) or "0.0.0.0"


# --------------------------------------------------------------------------
# day-0: via de seriële console
# --------------------------------------------------------------------------
def bootstrap_config(topo, state, site) -> str:
    """Minimale config bij de eerste boot (ingetypt op de console): hostname,
    interfaces, route, DNS en het admin-wachtwoord. De rest komt via SSH, zodat
    fouten per regel zichtbaar zijn in de UI."""
    a = adc(topo, site)
    wan = topo["fortiadc"]["wan"]
    lines = ["config system global", f"    set hostname {a['hostname']}", "end",
             "config system interface", '    edit "port1"']
    if wan["mode"] == "dhcp":
        lines.append("        set mode dhcp")
    else:
        lines += ["        set mode static", f"        set ip {a['mgmt']}"]
    lines += ["        set allowaccess https ssh ping http", "    next"]
    for p in a["ports"]:
        lines += [f'    edit "{p["port"]}"', "        set mode static", f"        set ip {p['ip']}",
                  "        set allowaccess ping", "    next"]
    lines.append("end")
    if wan["mode"] == "static" and wan.get("gateway"):
        lines += ["config router static", "    edit 1", f"        set gateway {wan['gateway']}",
                  "    next", "end"]
    dns = [d for d in (wan.get("dns1"), wan.get("dns2")) if d]
    if dns:
        lines += ["config system dns", f"    set primary {dns[0]}"]
        if len(dns) > 1:
            lines.append(f"    set secondary {dns[1]}")
        lines.append("end")
    if state.get("adc_admin_password"):
        lines += ["config system admin", '    edit "admin"',
                  f"        set password {state['adc_admin_password']}", "    next", "end"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# server load balancing (per FortiADC)
# --------------------------------------------------------------------------
def _host_app(vs, host) -> str:
    """Welke app een FQDN bedient: de pool van de host-route, of de VS-pool."""
    for r in vs.get("routes", []):
        if r["match"] == "host" and r["value"] == host:
            return r["pool"]
    return vs.get("pool") or host


def _vs_apps(vs) -> list[str]:
    return [vs["pool"]] if vs.get("pool") else list(dict.fromkeys(r["pool"] for r in vs["routes"]))


def health_checks(topo, site) -> list[str]:
    entries = []
    apps = list(dict.fromkeys(app for vs in site_vses(topo, site) for app in _vs_apps(vs)))
    for app in apps:
        h = topo["apps"][app]["health"]
        entries.append((hc_name(app), [
            "set type http", "set port 80", "set method-type http_get",
            f"set send-string {h['path']}", f"set receive-string {q(h['expect'])}",
            "set status-code 200", "set match-type match_all",
            "set interval 5", "set timeout 3"]))
    # GSLB (Generic Host): HTTPS naar de VIP met de juiste Host-header. Op beide
    # FortiADC's, want beide controleren beide datacenters.
    for vs in gslb_vses(topo, "generic"):
        for host in vs["hosts"]:
            h = topo["apps"][_host_app(vs, host)]["health"]
            entries.append((gslb_hc_name(host), [
                "set type https", "set port 443", "set method-type http_get",
                "set http-version http_1.1", f"set hostname {host}.{domain(topo)}",
                f"set send-string {h['path']}", f"set receive-string {q(h['expect'])}",
                "set status-code 200", "set match-type match_all",
                "set interval 10", "set timeout 5"]))
    return _block("system health-check", entries)


def real_servers(topo, site) -> list[str]:
    seg = datacenter_map(topo)[site]["segment"]
    return _block("load-balance real-server", [
        (f"RS-{n['name']}", [f"set ip {n['ip']}", "set status enable"])
        for n in inventory(topo) if n.get("app") and n["segment"] == seg])


def pools(topo, site) -> list[str]:
    entries = []
    apps = list(dict.fromkeys(app for vs in site_vses(topo, site) for app in _vs_apps(vs)))
    for app in apps:
        members = [(str(i + 1), [f"set pool_member_cookie {n['name']}", f"set real-server RS-{n['name']}"])
                   for i, n in enumerate(pool_members(topo, app, site))]
        body = ["set health-check-ctrl enable", f"set health-check-list {hc_name(app)}",
                "set real-server-ssl-profile NONE"] + _block("pool_member", members)
        entries.append((pool_name(app, site), body))
    return _block("load-balance pool", entries)


def persistence() -> list[str]:
    # Insert-cookie: de FortiADC zet zelf een cookie met de pool_member_cookie (de
    # servernaam) en stuurt de client daarna steeds naar dezelfde server.
    return _block("load-balance persistence", [
        (PERSISTENCE["cookie"], ["set type insert-cookie", "set keyword FADCLAB", "set timeout 3600"])])


def content_routes(topo, site) -> list[str]:
    entries = []
    for vs in site_vses(topo, site):
        for r in vs.get("routes", []):
            if r["match"] == "host":
                cond = ["set object http-host-header", "set type string",
                        f"set content {r['value']}.{domain(topo)}"]
            else:
                cond = ["set object http-request-url", "set type regular-expression",
                        f"set content {q('^' + r['value'])}"]
            body = ["set type l7-content-routing", f"set load-balance-pool {pool_name(r['pool'], site)}"]
            entries.append((route_name(vs, site, r), body + _block("match-condition", [("1", cond)])))
    return _block("load-balance content-routing", entries) if entries else []


def waf_profiles() -> list[str]:
    return _block("security waf profile", [
        # 'heuristic-sql-xss-injection-detect' is de naam in 8.0 (getest); de controller
        # valt terug op '...-detection' (oudere documentatie) als hij die weigert.
        (name, [f"set web-attack-signature {level}", f"set heuristic-sql-xss-injection-detect {level}",
                f"set desc {q(desc)}"])
        for name, (level, desc) in WAF_PROFILES.items()])


def client_port(topo, site) -> str:
    return next(p["port"] for p in adc(topo, site)["ports"] if p["segment"] == "client")


def virtual_servers(topo, site) -> list[str]:
    entries = []
    for vs in site_vses(topo, site):
        _, _, cssl = ssl_names(vs)
        body = ["set type l7-load-balance", f"set interface {client_port(topo, site)}",
                f"set ip {vs['sites'][site]}", "set port 443",
                "set load-balance-profile LB_PROF_HTTPS", f"set client-ssl-profile {cssl}",
                f"set load-balance-method {vs.get('method', 'LB_METHOD_ROUND_ROBIN')}"]
        if vs.get("routes"):
            body += ["set content-routing enable",
                     "set content-routing-list " + " ".join(route_name(vs, site, r) for r in vs["routes"])]
        else:
            body.append(f"set load-balance-pool {pool_name(vs['pool'], site)}")
        if vs.get("persistence"):
            body.append(f"set load-balance-persistence {PERSISTENCE[vs['persistence']]}")
        if vs.get("waf"):
            body.append(f"set waf-profile {vs['waf']}")
        body += ["set http2https enable", "set traffic-log enable", "set fortiview enable",
                 f"set comments {q(vs['title'][:60])}", "set status enable"]
        entries.append((vs_name(vs, site), body))
    return _block("load-balance virtual-server", entries)


# --------------------------------------------------------------------------
# GSLB (identiek op beide FortiADC's)
# --------------------------------------------------------------------------
def gslb_vses(topo, server=None) -> list[dict]:
    return [vs for vs in topo["virtual_servers"]
            if vs.get("gslb") and (server is None or vs["gslb"].get("server", "slb") == server)]


# De lokale FortiADC zit al in de GSLB als ingebouwde server DEFAULT_DNS_SERVER
# (127.0.0.1): die synchroniseert de eigen virtual servers. De ándere FortiADC komt
# erbij als FortiADC-SLB-server op zijn management-IP (TCP 5858, auto-sync).
LOCAL_SERVER = "DEFAULT_DNS_SERVER"
# Sync-authenticatie: TCP-MD5 vraagt een wachtwoord dat op de andere FortiADC in een
# (ongedocumenteerde) instelling moet staan. In het lab loopt de sync over het
# management-LAN; daarom voorlopig zonder.
GSLB_AUTH = "none"


def _member(vs, host, site, local) -> tuple[str, str]:
    """(GSLB-server, server-member-name) van een FQDN in een datacenter, gezien vanaf
    de FortiADC van datacenter 'local'."""
    if vs["gslb"].get("server", "slb") == "slb":
        server = LOCAL_SERVER if site == local else slb_server(site)
        return server, vs_name(vs, site)  # naam zoals auto-sync hem ophaalt
    return generic_server(site), host


def gslb(topo, local) -> list[str]:
    """GSLB-config voor de FortiADC van datacenter 'local'. Beide FortiADC's kennen
    dezelfde datacenters, pools en hosts; alleen wie 'lokaal' is verschilt."""
    dcs = datacenter_map(topo)
    lines = _block("global-load-balance data-center",
                   [(d["id"], [f"set location {d['location']}"]) for d in dcs.values()])
    servers = [(LOCAL_SERVER, [f"set data-center {local}"])]
    for site in dcs:
        if site != local:
            servers.append((slb_server(site), [
                "set server-type FortiADC-SLB", f"set ip {mgmt_ip(topo, site)}", "set port 5858",
                f"set data-center {site}", f"set auth-type {GSLB_AUTH}", "set auto-sync enable"]))
    for site in dcs:
        members = []
        for vs in gslb_vses(topo, "generic"):
            if site in vs["sites"]:
                members += [(host, [f"set ip {vs['sites'][site]}", "set health-check-inherit disable",
                                    "set health-check-ctrl enable",
                                    f"set health-check-list {gslb_hc_name(host)}"])
                            for host in vs["hosts"]]
        if members:
            body = ["set server-type Generic-Host", f"set data-center {site}"]
            servers.append((generic_server(site), body + _block("virtual-server-list", members)))
    lines += _block("global-load-balance servers", servers)

    pools_, hosts = [], []
    for vs in gslb_vses(topo):
        method = vs["gslb"]["method"]
        for host in vs["hosts"]:
            if method == "global-availability":
                # Eén pool per DC; de volgorde in de pool-lijst bepaalt wie actief is.
                names = []
                for site in vs["sites"]:
                    server, member = _member(vs, host, site, local)
                    names.append(f"GP-{up(host)}-{site}")
                    pools_.append((names[-1], _block("member", [
                        ("1", [f"set server {server}", f"set server-member-name {member}", "set weight 1"])])))
            else:
                names = [f"GP-{up(host)}"]
                members = []
                for i, site in enumerate(vs["sites"]):
                    server, member = _member(vs, host, site, local)
                    members.append((str(i + 1), [f"set server {server}", f"set server-member-name {member}",
                                                 "set weight 1"]))
                pools_.append((names[0], _block("member", members)))
            # FortiADC eist een FQDN met afsluitende punt.
            body = [f"set host-name {host}", f"set domain-name {gslb_domain(topo)}.",
                    f"set load-balance-method {method}"]
            body += _block("virtual-server-pool-list",
                           [(str(i + 1), [f"set virtual-server-pool {p}"]) for i, p in enumerate(names)])
            hosts.append((f"GH-{up(host)}", body))
    lines += _block("global-load-balance virtual-server-pool", pools_)
    lines += _block("global-load-balance host", hosts)
    # De global DNS-server staat in de fabrieksstand uit: zonder gds-status geeft poort 53
    # 'connection refused' en kan BIND de delegatie naar de FortiADC's niet volgen.
    lines += ["config global-dns-server general", "    set gds-status enable",
              "    set listen-on-all-interface enable", "end"]
    return lines


DEFAULT_POLICY = "DEFAULT_DNS_POLICY"


def dns_policy(zones: list[str]) -> list[str]:
    """Policy die de (door de GSLB-hosts gegenereerde) zones serveert. De zonenamen
    zijn pas na het aanmaken van de hosts bekend; de controller leest ze terug.
    Policies worden van boven naar beneden gematcht, en de ingebouwde
    DEFAULT_DNS_POLICY (any/any, recursief) staat bovenaan: DNS-LAB moet erboven."""
    lines = _block("global-dns-server policy", [
        ("DNS-LAB", ["set source-address any", "set destination-address any",
                     f"set zone-list {' '.join(zones)}", "set recursion-status disable"])])
    return lines[:-1] + [f'    move "DNS-LAB" before "{DEFAULT_POLICY}"', "end"]


def dns_policy_fallback(zones: list[str]) -> list[str]:
    """Als de FortiADC 'move' niet kent: de zones aan de standaardpolicy hangen en
    daar recursie uitzetten (de FortiADC is geen open resolver voor het lab)."""
    return _block("global-dns-server policy", [
        (DEFAULT_POLICY, [f"set zone-list {' '.join(zones)}", "set recursion-status disable"])])


# --------------------------------------------------------------------------
# alles in volgorde
# --------------------------------------------------------------------------
def slb_blocks(topo, site) -> list[tuple[str, list[str]]]:
    """Volgorde = afhankelijkheden: wat verwezen wordt, eerst."""
    return [b for b in [
        ("Health checks", health_checks(topo, site)),
        ("Real servers", real_servers(topo, site)),
        ("Real server pools", pools(topo, site)),
        ("Persistence", persistence()),
        ("Content routing", content_routes(topo, site)),
        ("WAF-profielen", waf_profiles()),
        ("Virtual servers", virtual_servers(topo, site)),
    ] if b[1]]


def gslb_blocks(topo, site) -> list[tuple[str, list[str]]]:
    return [("GSLB (data centers, servers, pools, hosts)", gslb(topo, site))] if gslb_vses(topo) else []


def gslb_cleanup(site) -> list[str]:
    """Server die een eerdere versie aanmaakte: de FortiADC naar zichzelf (bleef leeg)."""
    return ["config global-load-balance servers", f'delete "{slb_server(site)}"', "end"]


def render(topo) -> str:
    """De volledige config van beide FortiADC's als tekst, voor de UI en de demo."""
    out = ["# FortiADC Lab - gegenereerd uit lab/topology.yaml",
           "# Certificaten (" + ", ".join(cert_name(v) for v in topo["virtual_servers"]) +
           "), certificaatgroepen en client-SSL-profielen gaan via de REST API.", ""]
    for a in topo["adcs"]:
        out += ["#" * 78, f"# {a['hostname']}  (datacenter {a['dc']}, management {a['mgmt'] or 'DHCP'})",
                "#" * 78, ""]
        blocks = slb_blocks(topo, a["site"]) + gslb_blocks(topo, a["site"])
        for title, lines in blocks:
            out += [f"# --- {title} ---", *lines, ""]
        out += ["# --- DNS-policy (zonenamen worden na de GSLB-hosts uitgelezen) ---",
                *dns_policy([f"{gslb_domain(topo)}."]), ""]
    return "\n".join(out)

