import threading
import uuid
from contextlib import closing

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import config
from ..anki import targets
from ..anki.client import AnkiClient, AnkiError
from ..anki.service import (
    audit_target,
    list_conflicts,
    pending_push_count,
    pull_target,
    push_targets,
    resolve_conflict,
    summary,
    sync_all_targets,
    sync_model_style,
    takeover_target,
    undo_conflict,
)
from ..db import connect, utc_now
from .settings import load_settings

router = APIRouter(prefix="/api/anki", tags=["anki"])

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_ACTIVE = ("queued", "running")
ACTIONS = {"takeover", "audit", "push", "pull", "sync"}


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


def _enabled_target_ids(target_id: int | None) -> list[int]:
    with closing(connect()) as conn:
        rows = targets.list_targets(conn, enabled_only=True)
    ids = [int(row["id"]) for row in rows]
    if target_id is not None:
        if target_id not in ids:
            raise ValueError("同步目标不存在或未启用")
        return [target_id]
    return ids


def _run_action(action: str, settings: dict, target_id: int | None, progress):
    model = settings["anki_model"]
    enabled = settings["anki_sync_enabled"]
    guard = bool(settings.get("anki_conflict_guard", True))
    if action == "push":
        return push_targets(
            model, enabled, guard, progress, _enabled_target_ids(target_id)
        )
    if action == "pull":
        ids = _enabled_target_ids(target_id)
        results = []
        for item in ids:
            result = pull_target(item, model, enabled, guard, progress)
            result["action"] = "pull"
            results.append(result)
        return results[0] if len(results) == 1 else {"targets": results}
    if action in ("takeover", "audit"):
        ids = _enabled_target_ids(target_id)
        results = []
        for item in ids:
            try:
                if action == "takeover":
                    results.append(takeover_target(item, model, guard, progress))
                else:
                    results.append(audit_target(item, model, guard, progress))
            except Exception as exc:  # noqa: BLE001 - 单目标失败不阻塞
                results.append(
                    {
                        "target_id": item,
                        "target": str(item),
                        "errors": [str(exc)],
                        "matched": 0,
                        "missing_total": 0,
                    }
                )
        return results[0] if len(results) == 1 else {"targets": results}
    return sync_all_targets(
        model, enabled, guard, progress, _enabled_target_ids(target_id)
    )


def _run_job(
    job_id: str, action: str, settings: dict, target_id: int | None
) -> None:
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
    try:
        result = _run_action(action, settings, target_id, progress)
        _set(job_id, status="done", result=result, finished_at=utc_now())
    except Exception as exc:
        _set(job_id, status="failed", error=str(exc), finished_at=utc_now())
    finally:
        timer.cancel()


def _start(action: str, target_id: int | None = None) -> dict:
    if action not in ACTIONS:
        raise HTTPException(status_code=400, detail="不支持的任务")
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
            "target_id": target_id,
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
        target=_run_job,
        args=(job_id, action, settings, target_id),
        daemon=True,
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


def _target_payload(conn, row) -> dict:
    item = dict(row)
    target_id = int(row["id"])
    item["group_ids"] = targets.target_group_ids(conn, target_id)
    item.update(targets.target_stats(conn, target_id))
    if row["enabled"]:
        # 与顶部汇总保持一致：按内容哈希判断，已推送但内容变动也算待推送
        item["pending"] = pending_push_count(conn, target_id)
    return item


@router.get("/status")
def get_status() -> dict:
    settings = load_settings()
    with closing(connect()) as conn:
        rows = targets.list_targets(conn)
        target_items = [_target_payload(conn, row) for row in rows]

    primary = next((row for row in rows if row["enabled"]), None)
    status = {
        "url": primary["base_url"] if primary else None,
        "model": settings["anki_model"],
        "sync_enabled": settings["anki_sync_enabled"],
        "conflict_guard": bool(settings.get("anki_conflict_guard", True)),
        "connected": False,
        "version": None,
        "fields": None,
        "error": None,
        "targets": target_items,
    }
    if primary is not None:
        client = AnkiClient(
            primary["base_url"],
            conflict_guard=bool(settings.get("anki_conflict_guard", True)),
        )
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

    return {
        **status,
        **summary(),
        "targets": target_items,
        "backups": _backups(),
        "running_job": running,
    }


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    base_url: str = Field(min_length=1, max_length=500)
    enabled: bool = True


class TargetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    enabled: bool | None = None
    sort: int | None = Field(default=None, ge=0)


class TargetGroupsIn(BaseModel):
    group_ids: list[int] = Field(default_factory=list, max_length=2000)


def _check_url(base_url: str) -> str:
    value = base_url.strip()
    if not value.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="地址必须以 http:// 或 https:// 开头")
    return value.rstrip("/")


@router.get("/targets")
def get_targets() -> dict:
    with closing(connect()) as conn:
        rows = targets.list_targets(conn)
        return {
            "items": [_target_payload(conn, row) for row in rows],
            "groups": targets.all_groups(conn),
        }


@router.post("/targets")
def create_target(payload: TargetIn) -> dict:
    with closing(connect()) as conn, conn:
        item = targets.create_target(
            conn, payload.name, _check_url(payload.base_url), payload.enabled
        )
        return _target_payload(conn, targets.get_target(conn, item["id"]))


@router.patch("/targets/{target_id}")
def update_target(target_id: int, payload: TargetPatch) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "base_url" in updates:
        updates["base_url"] = _check_url(updates["base_url"])
    with closing(connect()) as conn, conn:
        if targets.get_target(conn, target_id) is None:
            raise HTTPException(status_code=404, detail="同步目标不存在")
        row = targets.update_target(conn, target_id, **updates)
        return _target_payload(conn, row)


@router.delete("/targets/{target_id}")
def delete_target(target_id: int) -> dict:
    with closing(connect()) as conn, conn:
        if not targets.delete_target(conn, target_id):
            raise HTTPException(status_code=404, detail="同步目标不存在")
    return {"deleted": target_id}


@router.put("/targets/{target_id}/groups")
def set_target_groups(target_id: int, payload: TargetGroupsIn) -> dict:
    with closing(connect()) as conn, conn:
        if targets.get_target(conn, target_id) is None:
            raise HTTPException(status_code=404, detail="同步目标不存在")
        groups = targets.set_target_groups(conn, target_id, payload.group_ids)
        return {"target_id": target_id, "group_ids": groups}


class ModelStyleIn(BaseModel):
    source_target_id: int | None = None


@router.post("/targets/{target_id}/style")
def copy_model_style(target_id: int, payload: ModelStyleIn | None = None) -> dict:
    settings = load_settings()
    try:
        return sync_model_style(
            target_id,
            settings["anki_model"],
            payload.source_target_id if payload else None,
            guard=bool(settings.get("anki_conflict_guard", True)),
            sync_enabled=bool(settings.get("anki_sync_enabled")),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, AnkiError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/targets/{target_id}/status")
def get_target_status(target_id: int) -> dict:
    settings = load_settings()
    with closing(connect()) as conn:
        row = targets.get_target(conn, target_id)
        if row is None:
            raise HTTPException(status_code=404, detail="同步目标不存在")
        item = _target_payload(conn, row)
    item.update(connected=False, version=None, fields=None, error=None)
    client = AnkiClient(
        row["base_url"],
        conflict_guard=bool(settings.get("anki_conflict_guard", True)),
    )
    try:
        info = client.validate(settings["anki_model"], timeout=8)
        item.update(
            connected=True, version=info["version"], fields=info["fields"]
        )
    except AnkiError as exc:
        item["error"] = str(exc)
    return item


@router.post("/takeover")
def start_takeover(target_id: int | None = None) -> dict:
    return _start("takeover", target_id)


@router.post("/audit")
def start_audit(target_id: int | None = None) -> dict:
    return _start("audit", target_id)


class NoteDeleteIn(BaseModel):
    target_id: int
    note_ids: list[int] = Field(min_length=1, max_length=500)


@router.post("/notes/delete")
def delete_notes(payload: NoteDeleteIn) -> dict:
    note_ids = list(dict.fromkeys(payload.note_ids))
    with closing(connect()) as conn:
        target = targets.get_target(conn, payload.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="同步目标不存在")
    client = AnkiClient(target["base_url"])
    try:
        client.call("deleteNotes", timeout=15, notes=note_ids)
    except (AnkiError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"Anki 删除失败：{exc}"
        ) from exc
    return {"deleted": note_ids, "target_id": payload.target_id}


@router.post("/push")
def start_push(target_id: int | None = None) -> dict:
    return _start("push", target_id)


@router.post("/sync")
def start_sync(target_id: int | None = None) -> dict:
    return _start("sync", target_id)


@router.post("/pull")
def start_pull(target_id: int | None = None) -> dict:
    return _start("pull", target_id)


@router.get("/conflicts")
def get_conflicts(status: str = "pending", limit: int = 100) -> dict:
    try:
        return list_conflicts(status, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ConflictResolveIn(BaseModel):
    choice: str = Field(pattern="^(system|target)$")


@router.post("/conflicts/{conflict_id}/resolve")
def resolve(conflict_id: int, payload: ConflictResolveIn) -> dict:
    try:
        return resolve_conflict(conflict_id, payload.choice)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/conflicts/{conflict_id}/undo")
def undo(conflict_id: int) -> dict:
    try:
        return undo_conflict(conflict_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/logs")
def get_logs(
    status: str | None = None, limit: int = 50, target_id: int | None = None
) -> dict:
    where = []
    params: list = []
    if status == "failed":
        where.append("s.last_error IS NOT NULL")
    elif status == "pushed":
        where.append("s.last_error IS NULL AND s.pushed_at IS NOT NULL")
    if target_id is not None:
        where.append("s.target_id = ?")
        params.append(target_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = max(1, min(limit, 200))

    with closing(connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT s.kp_id, s.target_id, t.name AS target_name, s.anki_note_id,
                   s.pushed_hash, s.pushed_at, s.last_error, s.pushed_deck,
                   s.detached, kp.front_md, kp.status AS card_status
            FROM anki_sync s
            JOIN knowledge_point kp ON kp.id = s.kp_id
            LEFT JOIN anki_target t ON t.id = s.target_id
            {clause}
            ORDER BY COALESCE(s.pushed_at, '') DESC, s.kp_id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS value FROM anki_sync s {clause}",
            params,
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
