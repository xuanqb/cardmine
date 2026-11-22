import threading
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import config
from ..anki import (
    AnkiClient,
    AnkiError,
    audit,
    list_conflicts,
    pull,
    push,
    resolve_conflict,
    sync_anki,
    takeover,
    undo_conflict,
)
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
        if job is None or job.get("cancelled"):
            return
        job.update(fields)
        job["updated_at"] = utc_now()


def _fail_job(job_id: str, reason: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job["status"] not in _ACTIVE:
            return
        job.update(
            status="failed",
            error=reason,
            cancelled=True,
            finished_at=utc_now(),
            updated_at=utc_now(),
        )


def _run_job(job_id: str, action: str, settings: dict) -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _set(job_id, phase=phase, processed=done, total=total)

    _set(job_id, status="running", started_at=utc_now())
    timer = threading.Timer(
        config.ANKI_JOB_TIMEOUT_SECONDS,
        _fail_job,
        args=(
            job_id,
            f"任务超过 {config.ANKI_JOB_TIMEOUT_SECONDS} 秒未完成，已标记失败",
        ),
    )
    timer.daemon = True
    timer.start()

    client = AnkiClient(settings["anki_url"] or config.ANKI_CONNECT_URL)
    try:
        if action == "takeover":
            result = takeover(client, settings["anki_model"], progress)
        elif action == "audit":
            result = audit(client, settings["anki_model"], progress)
        elif action == "push":
            result = push(
                client,
                settings["anki_model"],
                settings["anki_sync_enabled"],
                progress,
            )
        elif action == "pull":
            result = pull(client, settings["anki_model"], progress)
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
    finally:
        timer.cancel()


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
            "updated_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "cancelled": False,
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


@router.post("/audit")
def start_audit() -> dict:
    return _start("audit")


class NoteDeleteIn(BaseModel):
    note_ids: list[int] = Field(min_length=1, max_length=500)


@router.post("/notes/delete")
def delete_notes(payload: NoteDeleteIn) -> dict:
    note_ids = list(dict.fromkeys(payload.note_ids))
    settings = load_settings()
    client = AnkiClient(settings["anki_url"] or config.ANKI_CONNECT_URL)
    try:
        client.call("deleteNotes", timeout=15, notes=note_ids)
    except (AnkiError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"Anki 删除失败：{exc}"
        ) from exc
    return {"deleted": note_ids}


@router.post("/push")
def start_push() -> dict:
    return _start("push")


@router.post("/sync")
def start_sync() -> dict:
    return _start("sync")


@router.post("/pull")
def start_pull() -> dict:
    return _start("pull")


@router.get("/conflicts")
def get_conflicts(status: str = "pending", limit: int = 100) -> dict:
    try:
        return list_conflicts(status, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ConflictResolveIn(BaseModel):
    choice: str = Field(pattern="^(system|anki)$")


@router.post("/conflicts/{conflict_id}/resolve")
def resolve(conflict_id: int, payload: ConflictResolveIn) -> dict:
    settings = load_settings()
    client = AnkiClient(settings["anki_url"] or config.ANKI_CONNECT_URL)
    try:
        return resolve_conflict(client, conflict_id, payload.choice)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AnkiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/conflicts/{conflict_id}/undo")
def undo(conflict_id: int) -> dict:
    try:
        return undo_conflict(conflict_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="同步任务不存在")
        if job["status"] in _ACTIVE:
            job.update(
                status="failed",
                error="用户取消",
                cancelled=True,
                finished_at=utc_now(),
                updated_at=utc_now(),
            )
        return dict(job)
