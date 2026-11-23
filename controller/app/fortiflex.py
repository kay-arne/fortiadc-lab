"""FortiFlex (v2) via directe API-toegang: configuraties, entitlements en tokens voor de FortiADC's.

Zelfde aanpak als in sdwan-aiassist-lab; hier één producttype (FortiADC-VM) en één entitlement per datacenter.

De controller regelt zelf de levenscyclus van de licenties, zodat je bij een nieuwe uitrol geen tokens
meer hoeft te maken:

  uitrollen   -> entitlement aanmaken (of hergebruiken: reactiveren + nieuwe token) en de token in de VM zetten
  verwijderen -> entitlement stoppen (geen punten meer verbruiken); de entitlement blijft bestaan voor hergebruik

Authenticatie: een API-gebruiker uit de FortiCloud IAM-portal (rechten op FortiFlex), grant_type password,
client_id 'flexvm'. Het access-token wordt gecached en bij 'Invalid security token' opnieuw opgehaald.
Endpoints en parameter-ID's volgen Fortinets eigen Ansible/Terraform-collecties voor FortiFlex v2."""
from __future__ import annotations

import threading
import time

import requests

from .config import get_state, update_state

AUTH_URL = "https://customerapiauth.fortinet.com/api/v1/oauth/token/"
API_URL = "https://support.fortinet.com/ES/api/fortiflex/v2/"

# Producttype- en parameter-ID's (FortiFlex v2), volgens Fortinets Terraform-provider (config.go: FAD_VM = 9,
# fad_vm.cpu_size = 25, fad_vm.service_pkg = 26) en de Ansible-collectie (waarden).
#   cpu:     "1", "2", "4", "8", "16", "32"
#   service: FDVFC247 (FortiCare Premium), FDVNET (Network Security), FDVAPP (Application Security), FDVAI (AI Security)
PRODUCTS = {
    "fad": {"product": 9, "params": {"cpu": 25, "service": 26}},
}
DEFAULTS = {
    "fad": {"service": "FDVAPP"},       # Application Security: WAF-signatures voor de demo's
}
LABEL = {"fad": "FortiADC-VM"}

_lock = threading.Lock()
_token = {"value": "", "key": "", "at": 0.0}


class FlexError(RuntimeError):
    pass


# --- toegang ---------------------------------------------------------------------------
def creds() -> dict:
    c = get_state().get("fortiflex_api") or {}
    if not c.get("username") or not c.get("password"):
        raise FlexError("FortiFlex API-gebruiker en wachtwoord zijn niet ingesteld (Setup > Licentie).")
    return c


def is_api_mode() -> bool:
    return get_state().get("license_mode") == "fortiflex_api"


def _login(c: dict) -> str:
    r = requests.post(AUTH_URL, json={"username": c["username"], "password": c["password"],
                                      "client_id": "flexvm", "grant_type": "password"}, timeout=30)
    if r.status_code >= 400:
        raise FlexError(f"FortiFlex-login mislukt (HTTP {r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def call(path: str, data: dict | None = None) -> dict:
    c = creds()
    key = c["username"] + c["password"]
    for attempt in (1, 2):
        with _lock:
            if not _token["value"] or _token["key"] != key or time.time() - _token["at"] > 1500:
                _token.update(value=_login(c), key=key, at=time.time())
            bearer = _token["value"]
        r = requests.post(API_URL + path, json=data or {}, headers={"Authorization": f"Bearer {bearer}"}, timeout=60)
        try:
            body = r.json()
        except ValueError:
            raise FlexError(f"{path}: HTTP {r.status_code}: {r.text[:200]}") from None
        if body.get("status") == -1 and "security token" in str(body.get("message", "")).lower() and attempt == 1:
            _token["value"] = ""
            continue
        if r.status_code >= 400 or body.get("status", 0) not in (0, None):
            raise FlexError(f"{path}: {body.get('message') or body}")
        return body
    raise FlexError(f"{path}: authenticatie mislukt")


def _one(x):
    return (x[0] if x else {}) if isinstance(x, list) else (x or {})


def program_serial() -> str:
    c = creds()
    if c.get("program_serial"):
        return c["program_serial"]
    progs = call("programs/list").get("programs") or []
    if not progs:
        raise FlexError("Geen FortiFlex-programma gevonden voor deze API-gebruiker.")
    return progs[0]["serialNumber"]


def _base(c: dict) -> dict:
    out = {}
    if c.get("account_id"):
        out["accountId"] = int(c["account_id"])
    return out


# --- configuraties ---------------------------------------------------------------------
def config_name(topo, kind: str) -> str:
    return f"{topo['lab']['name_prefix']}-{kind}"


def spec(topo, kind: str) -> dict:
    """Parameters van de lab-configuratie. De CPU-grootte volgt fortiadc.cores (de VM moet passen)."""
    base = {**DEFAULTS[kind], "cpu": str(topo["fortiadc"]["cores"])}
    return {**base, **((topo.get("fortiflex") or {}).get(kind) or {})}


def _parameters(kind: str, values: dict) -> list[dict]:
    ids, out = PRODUCTS[kind]["params"], []
    for name, value in values.items():
        if name not in ids or value in (None, "", []):
            continue
        for v in (value if isinstance(value, list) else [value]):
            out.append({"id": ids[name], "value": v})
    return out


def ensure_config(log, topo, kind: str) -> int:
    """Zoek de lab-configuratie op naam of maak hem aan (parameters uit topology.yaml > fortiflex)."""
    c = creds()
    name = config_name(topo, kind)
    cfgs = call("configs/list", {"programSerialNumber": program_serial(), **_base(c)}).get("configs") or []
    for cfg in cfgs:
        if cfg.get("name") == name:
            return int(cfg["id"])
    values = spec(topo, kind)
    res = call("configs/create", {"programSerialNumber": program_serial(), "name": name,
                                  "productTypeId": PRODUCTS[kind]["product"],
                                  "parameters": _parameters(kind, values), **_base(c)})
    cfg = _one(res.get("configs"))
    log(f"[FortiFlex] configuratie {name} aangemaakt ({LABEL[kind]}: {values})")
    return int(cfg["id"])


# --- entitlements ----------------------------------------------------------------------
def _kind(key: str) -> str:
    return key.split(":")[0]


def _desc(topo, key: str) -> str:
    return f"{topo['lab']['name_prefix']} {key}"


def _list(config_id: int, **filters) -> list[dict]:
    """entitlements/list vereist configId (of accountId + programSerialNumber)."""
    return call("entitlements/list", {"configId": config_id, **filters}).get("entitlements") or []


def _find(topo, key: str, config_id: int) -> dict | None:
    serial = (get_state().get("fortiflex_entitlements") or {}).get(key)
    if serial:
        found = _list(config_id, serialNumber=serial)
        if found:
            return found[0]
    for e in _list(config_id):          # na verlies van state: terugvinden op beschrijving
        if e.get("description") == _desc(topo, key):
            return e
    return None


def _remember(key: str, serial: str):
    ents = dict(get_state().get("fortiflex_entitlements") or {})
    ents[key] = serial
    update_state(fortiflex_entitlements=ents)


def acquire(log, topo, key: str) -> str:
    """Geef een geldige, ongebruikte VM-token voor het apparaat (key: fad:<site>)."""
    kind = _kind(key)
    config_id = ensure_config(log, topo, kind)
    ent = _find(topo, key, config_id)
    if ent and ent.get("status") == "EXPIRED":
        ent = None
    if not ent:
        res = call("entitlements/vm/create", {"configId": config_id, "count": 1, "description": _desc(topo, key)})
        ent = _one(res.get("entitlements"))
        _remember(key, ent["serialNumber"])
        log(f"[FortiFlex] entitlement {ent['serialNumber']} aangemaakt voor {key}")
        return ent["token"]
    _remember(key, ent["serialNumber"])
    if ent.get("status") == "STOPPED":
        call("entitlements/reactivate", {"serialNumber": ent["serialNumber"]})
        log(f"[FortiFlex] entitlement {ent['serialNumber']} ({key}) gereactiveerd")
        ent = (_list(config_id, serialNumber=ent["serialNumber"]) or [ent])[0]
    if ent.get("tokenStatus") == "USED" or not ent.get("token"):
        res = call("entitlements/vm/token", {"serialNumber": ent["serialNumber"]})
        ent = _one(res.get("entitlements")) or ent
        log(f"[FortiFlex] nieuwe token voor {key} ({ent['serialNumber']})")
    return ent["token"]


def release(log, key: str):
    """Entitlement stoppen (geen punten meer); wordt bij de volgende uitrol gereactiveerd."""
    serial = (get_state().get("fortiflex_entitlements") or {}).get(key)
    if not serial:
        return
    try:
        call("entitlements/stop", {"serialNumber": serial})
        log(f"[FortiFlex] entitlement {serial} ({key}) gestopt")
    except FlexError as e:
        log(f"⚠ [FortiFlex] stoppen van {serial} ({key}) mislukt: {e}")


def release_all(log):
    for key in list((get_state().get("fortiflex_entitlements") or {})):
        release(log, key)


def _config_ids(topo) -> dict:
    c = creds()
    cfgs = call("configs/list", {"programSerialNumber": program_serial(), **_base(c)}).get("configs") or []
    by_name = {cf.get("name"): int(cf["id"]) for cf in cfgs}
    return {k: by_name.get(config_name(topo, k)) for k in PRODUCTS}


def status(topo) -> list[dict]:
    out, ids = [], _config_ids(topo)
    for key, serial in (get_state().get("fortiflex_entitlements") or {}).items():
        cid = ids.get(_kind(key))
        found = _list(cid, serialNumber=serial) if cid else []
        e = found[0] if found else {}
        out.append({"device": key, "serial": serial, "status": e.get("status", "onbekend"),
                    "token_status": e.get("tokenStatus", ""), "config_id": e.get("configId")})
    return out


def test(topo) -> dict:
    """Verbinding testen: programma's en bestaande lab-configuraties."""
    c = creds()
    progs = call("programs/list").get("programs") or []
    cfgs = call("configs/list", {"programSerialNumber": program_serial(), **_base(c)}).get("configs") or []
    names = {config_name(topo, k): k for k in PRODUCTS}
    return {"programs": [p.get("serialNumber") for p in progs],
            "program_used": program_serial(),
            "lab_configs": {n: any(cf.get("name") == n for cf in cfgs) for n in names}}


# --- beheer per apparaat (Setup > Licentie) -----------------------------------------------
def device_keys(topo) -> list[tuple[str, str]]:
    """(key, label) van alle apparaten die een licentie nodig hebben."""
    return [(f"fad:{a['site']}", f"{a['hostname']} ({a['dc']})") for a in topo["adcs"]]


def entitlements(topo) -> list[dict]:
    """Eén regel per apparaat, met de actuele status uit FortiFlex (alleen lezen; maakt niets aan)."""
    ids = _config_ids(topo)
    known = get_state().get("fortiflex_entitlements") or {}
    cache: dict[int, list[dict]] = {}
    rows = []
    for key, label in device_keys(topo):
        cid = ids.get(_kind(key))
        found = None
        if cid:
            if cid not in cache:
                cache[cid] = _list(cid)
            serial = known.get(key)
            found = next((e for e in cache[cid] if serial and e.get("serialNumber") == serial), None) \
                or next((e for e in cache[cid] if e.get("description") == _desc(topo, key)), None)
            if found and known.get(key) != found["serialNumber"]:
                _remember(key, found["serialNumber"])
        rows.append({"device": key, "label": label, "config": bool(cid),
                     "serial": (found or {}).get("serialNumber", ""), "status": (found or {}).get("status", ""),
                     "token_status": (found or {}).get("tokenStatus", ""), "end_date": (found or {}).get("endDate", "")})
    return rows


def create(log, topo, key: str) -> dict:
    """Entitlement voor een apparaat aanmaken (of de bestaande teruggeven); gebruikt de token nog niet."""
    config_id = ensure_config(log, topo, _kind(key))
    ent = _find(topo, key, config_id)
    if ent and ent.get("status") != "EXPIRED":
        _remember(key, ent["serialNumber"])
        log(f"[FortiFlex] {key}: entitlement {ent['serialNumber']} bestaat al")
        return ent
    res = call("entitlements/vm/create", {"configId": config_id, "count": 1, "description": _desc(topo, key)})
    ent = _one(res.get("entitlements"))
    _remember(key, ent["serialNumber"])
    log(f"[FortiFlex] {key}: entitlement {ent['serialNumber']} aangemaakt")
    return ent


def _serial(key: str) -> str:
    serial = (get_state().get("fortiflex_entitlements") or {}).get(key)
    if not serial:
        raise FlexError(f"{key}: nog geen entitlement; maak er eerst een aan.")
    return serial


def new_token(log, key: str):
    serial = _serial(key)
    call("entitlements/vm/token", {"serialNumber": serial})
    log(f"[FortiFlex] {key}: nieuwe token voor {serial}")


def reactivate(log, key: str):
    serial = _serial(key)
    call("entitlements/reactivate", {"serialNumber": serial})
    log(f"[FortiFlex] {key}: entitlement {serial} gereactiveerd")


def unlink(log, key: str):
    """Koppeling in de controller verwijderen. FortiFlex kent geen 'verwijderen' voor VM-entitlements: stop de
    entitlement eerst (dan kost hij niets) en verwijder de koppeling; een nieuwe uitrol maakt dan een nieuwe aan."""
    ents = dict(get_state().get("fortiflex_entitlements") or {})
    serial = ents.pop(key, None)
    update_state(fortiflex_entitlements=ents)
    log(f"[FortiFlex] {key}: koppeling met {serial or '-'} verwijderd")


def activate(log, topo, key: str):
    """Zorg dat de entitlement van een apparaat ACTIVE is: aanmaken (direct actief), of een gestopte reactiveren."""
    config_id = ensure_config(log, topo, _kind(key))
    ent = _find(topo, key, config_id)
    if not ent or ent.get("status") == "EXPIRED":
        res = call("entitlements/vm/create", {"configId": config_id, "count": 1, "description": _desc(topo, key),
                                              "skipPending": True})
        ent = _one(res.get("entitlements"))
        _remember(key, ent["serialNumber"])
        log(f"[FortiFlex] {key}: entitlement {ent['serialNumber']} aangemaakt (actief)")
        return
    _remember(key, ent["serialNumber"])
    status = ent.get("status")
    if status == "ACTIVE":
        return
    try:
        call("entitlements/reactivate", {"serialNumber": ent["serialNumber"]})
        log(f"[FortiFlex] {key}: entitlement {ent['serialNumber']} {status} -> ACTIVE")
    except FlexError as e:
        log(f"⚠ [FortiFlex] {key}: {ent['serialNumber']} staat op {status} en kon niet geactiveerd worden ({e}); "
            "wordt actief zodra de token wordt gebruikt")


def activate_all(log, topo, keys=None):
    """Alle (of de opgegeven) entitlements van dit lab op ACTIVE zetten, vóór de uitrol."""
    wanted = set(keys) if keys is not None else None
    log("== FortiFlex: entitlements activeren ==")
    for key, _ in device_keys(topo):
        if wanted is None or key in wanted:
            activate(log, topo, key)
