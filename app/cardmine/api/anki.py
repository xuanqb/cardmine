import threading
import uuid

from fastapi import APIRouter, HTTPException

from .. import config
from ..anki import AnkiClient, AnkiError, push, sync_anki, takeover
from ..anki.service import summary
from ..db import connect, utc_now
from .settings import load_settings

router = APIRouter(prefix="/api/anki", tags=["anki"])

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_ACTIVE = ("queued", "running")


def _set(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _run_job(job_id: str, action: str, settings: dict) -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _set(job_id, phase=phase, processed=done, total=total)

    _set(job_id, status="running", started_at=utc_now())
    client = AnkiClient(settings["anki_url"] or config.ANKI_CONNECT_URL)
    try:
        if action == "takeover":
            result = takeover(client, settings["anki_model"], progress)
        elif action == "push":
            result = push(
                client,
                settings["anki_model"],
                settings["anki_sync_enabled"],
                progress,
            )
        else:
            result = sync_anki(
                client,
                settings["anki_model"],
                settings["anki_sync_enabled"],
                progress,
            )
        _set(job_id, status="done", result=result, finished_at=utc_now())
    except Exception as exc:
        _set(job_id, status="failed", error=str(exc), finished_at=utc_now())


def _start(action: str) -> dict:
    with _lock:
        if any(job["status"] in _ACTIVE for job in _jobs.values()):
            raise HTTPException(status_code=409, detail="已有同步任务在运行，请稍候")
        if len(_jobs) > 20:
            for job_id in [
                key
                for key, job in _jobs.items()
                if job["status"] not in _ACTIVE
            ][: len(_jobs) - 20]:
                _jobs.pop(job_id, None)

        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {
            "job_id": job_id,
            "action": action,
            "status": "queued",
            "phase": "排队中",
            "processed": 0,
            "total": 0,
            "result": None,
            "error": None,
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
        }

    settings = load_settings()
    threading.Thread(
        target=_run_job, args=(job_id, action, settings), daemon=True
    ).start()
    return {"job_id": job_id}


def _backups(limit: int = 7) -> list[dict]:
    if not config.BACKUP_DIR.exists():
        return []
    items = sorted(
        (path for path in config.BACKUP_DIR.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )[:limit]
    result = []
    for path in items:
        collection = path / "collection.anki2"
        media = path / "collection.media"
        result.append(
            {
                "name": path.name,
                "size": collection.stat().st_size if collection.exists() else 0,
                "media": (
                    sum(1 for item in media.iterdir() if item.is_file())
                    if media.exists()
                    else 0
                ),
            }
        )
    return result


@router.get("/status")
def get_status() -> dict:
    settings = load_settings()
    client = AnkiClient(settings["anki_url"] or config.ANKI_CONNECT_URL)

    status = {
        "url": client.url,
        "model": settings["anki_model"],
        "sync_enabled": settings["anki_sync_enabled"],
        "connected": False,
        "version": None,
        "fields": None,
        "error": None,
    }
    try:
        info = client.validate(settings["anki_model"], timeout=8)
        status.update(
            connected=True, version=info["version"], fields=info["fields"]
        )
    except AnkiError as exc:
        status["error"] = str(exc)

    running = None
    with _lock:
        for job in _jobs.values():
            if job["status"] in _ACTIVE:
                running = dict(job)
                break

    return {**status, **summary(), "backups": _backups(), "running_job": running}


@router.post("/takeover")
def start_takeover() -> dict:
    return _start("takeover")


@router.post("/push")
def start_push() -> dict:
    return _start("push")


@router.post("/sync")
def start_sync() -> dict:
    return _start("sync")


@router.get("/logs")
def get_logs(status: str | None = None, limit: int = 50) -> dict:
    where = ""
    if status == "failed":
        where = "WHERE s.last_error IS NOT NULL"
    elif status == "pushed":
        where = "WHERE s.last_error IS NULL AND s.pushed_at IS NOT NULL"
    limit = max(1, min(limit, 200))

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT s.kp_id, s.anki_note_id, s.pushed_hash, s.pushed_at,
                   s.last_error, s.pushed_deck, kp.front_md, kp.status AS card_status
            FROM anki_sync s JOIN knowledge_point kp ON kp.id = s.kp_id
            {where}
            ORDER BY COALESCE(s.pushed_at, '') DESC, s.kp_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS value FROM anki_sync s {where}"
        ).fetchone()["value"]

    return {"total": total, "items": [dict(row) for row in rows]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="同步任务不存在")
        return dict(job)
