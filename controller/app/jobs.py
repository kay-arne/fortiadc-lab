"""Eenvoudige achtergrondjobs met een live log voor de web UI."""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid

from .config import DATA

JOBS_DIR = DATA / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

_jobs: dict[str, dict] = {}
_active_lock = threading.Lock()


class JobCancelled(BaseException):
    """Erft van BaseException zodat een brede `except Exception` in de job-functie
    hem niet opslokt; finally-blokken (opruimen) draaien wel."""


def _save_meta(job: dict):
    try:
        meta = {k: v for k, v in job.items() if k != "lines"}
        (JOBS_DIR / f"{job['id']}.json").write_text(json.dumps(meta))
    except OSError:
        pass


def _append(job: dict, msg: str):
    new = [f"{time.strftime('%H:%M:%S')} {line}" for line in (str(msg).rstrip().splitlines() or [""])]
    job["lines"].extend(new)
    try:
        with open(JOBS_DIR / f"{job['id']}.log", "a") as f:
            f.write("\n".join(new) + "\n")
    except OSError:
        pass


def _load_saved(keep=10):
    """Eerdere jobs terughalen na een herstart; een job die nog 'running' stond is onderbroken."""
    metas = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for i, mp in enumerate(metas):
        if i >= keep:
            for old in (mp, mp.with_suffix(".log")):
                old.unlink(missing_ok=True)
            continue
        try:
            job = json.loads(mp.read_text())
            lp = mp.with_suffix(".log")
            job["lines"] = lp.read_text().splitlines() if lp.exists() else []
        except (OSError, ValueError):
            continue
        if job.get("status") == "running":
            job["status"] = "interrupted"
            job["ended"] = job.get("ended") or time.time()
            job["lines"].append(f"{time.strftime('%H:%M:%S')} ✖ onderbroken: de controller is herstart tijdens deze job")
            _save_meta(job)
        _jobs[job["id"]] = job


def start(title: str, fn, *args, exclusive=True) -> dict:
    """Start fn(log, *args) in een thread. exclusive: maar één lab-wijzigende job tegelijk."""
    if exclusive and not _active_lock.acquire(blocking=False):
        raise RuntimeError("Er draait al een deploy/destroy-job.")
    job = {"id": uuid.uuid4().hex[:8], "title": title, "status": "running",
           "started": time.time(), "lines": [], "cancel": False}
    _jobs[job["id"]] = job
    _save_meta(job)

    def log(msg: str):
        if job["cancel"]:
            raise JobCancelled()
        _append(job, msg)

    def runner():
        try:
            fn(log, *args)
            job["status"] = "done"
            _append(job, "✔ klaar")
        except JobCancelled:
            job["status"] = "cancelled"
            _append(job, "✖ gestopt op verzoek")
        except Exception as e:  # noqa: BLE001 - alles naar de UI-log
            job["status"] = "failed"
            _append(job, f"✖ {e}")
            _append(job, traceback.format_exc())
        finally:
            job["ended"] = time.time()
            _save_meta(job)
            if exclusive:
                _active_lock.release()

    threading.Thread(target=runner, daemon=True).start()
    return job


def stop(job_id: str) -> bool:
    """Vraag een lopende job te stoppen; hij breekt af bij de eerstvolgende logregel
    (opruimen in finally-blokken draait nog wel). Geeft True als er een job stond."""
    j = _jobs.get(job_id)
    if j and j["status"] == "running":
        j["cancel"] = True
        _append(j, "… stoppen aangevraagd")
        return True
    return False


def get(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def recent(n=10) -> list[dict]:
    jobs = sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)[:n]
    return [{k: v for k, v in j.items() if k != "lines"} for j in jobs]


_load_saved()
