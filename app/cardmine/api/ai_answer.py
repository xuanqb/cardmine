"""AI 答案建议稿接口：单卡生成、草稿批量生成、任务进度与清空。"""

import asyncio
import threading
import uuid
from contextlib import closing, suppress
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..ai import AIError
from ..ai import answer
from ..db import connect, utc_now
from .cards import _module_ids, get_card
from .settings import load_settings

router = APIRouter(prefix="/api", tags=["ai-answer"])

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_tasks: dict[int, asyncio.Task] = {}
_ACTIVE = ("queued", "running")


class BatchIn(BaseModel):
    module_id: int | None = None
    limit: int = Field(20, ge=1, le=200)
    force: bool = False


def _set(job_id: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _draft_ids(conn, module_id: int | None, limit: int, force: bool) -> list[int]:
    where = ["kp.status = 'draft'"]
    params: list[Any] = []
    if not force:
        where.append("TRIM(COALESCE(kp.ai_back_md, '')) = ''")
    if module_id is not None:
        ids = _module_ids(conn, module_id)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        where.append(f"kp.module_id IN ({placeholders})")
        params.extend(ids)

    rows = conn.execute(
        f"SELECT kp.id FROM knowledge_point kp WHERE {' AND '.join(where)} "
        "ORDER BY kp.updated_at DESC, kp.id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [int(row["id"]) for row in rows]


@router.post("/cards/{card_id}/ai-answer")
async def generate_answer(card_id: int, force: bool = True) -> dict:
    settings = load_settings()
    running = _tasks.get(card_id)
    if running is not None and not running.done():
        raise HTTPException(status_code=409, detail="该卡片正在生成 AI 答案，请先取消")

    task = asyncio.create_task(
        answer.generate_for_card_async(settings, card_id, force=force)
    )
    _tasks[card_id] = task

    def forget(_task: asyncio.Task) -> None:
        if _tasks.get(card_id) is task:
            _tasks.pop(card_id, None)

    task.add_done_callback(forget)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if task.cancelled():
            return {"card_id": card_id, "cancelled": True}
        raise
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return get_card(card_id)


@router.post("/cards/{card_id}/ai-answer/cancel")
async def cancel_answer(card_id: int) -> dict:
    task = _tasks.get(card_id)
    if task is None or task.done():
        raise HTTPException(status_code=404, detail="该卡片没有正在进行的生成")
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return {"card_id": card_id, "cancelled": True}


@router.delete("/cards/{card_id}/ai-answer")
def clear_answer(card_id: int) -> dict:
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT id FROM knowledge_point WHERE id = ?", (card_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        conn.execute(
            "UPDATE knowledge_point SET ai_back_md = NULL, ai_status = 'none', "
            "ai_error = NULL, ai_generated_at = NULL WHERE id = ?",
            (card_id,),
        )
    return get_card(card_id)


def _run_batch(job_id: str, settings: dict, card_ids: list[int], force: bool) -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _set(job_id, phase=phase, processed=done, total=total)

    _set(job_id, status="running", started_at=utc_now())
    try:
        report = answer.run_batch(settings, card_ids, force=force, progress=progress)
        _set(job_id, status="done", report=report, finished_at=utc_now())
    except Exception as exc:  # noqa: BLE001
        _set(job_id, status="failed", error=str(exc), finished_at=utc_now())


@router.post("/ai-answer/batch")
def start_batch(payload: BatchIn) -> dict:
    settings = load_settings()
    with _lock:
        if any(job["status"] in _ACTIVE for job in _jobs.values()) or answer.is_running():
            raise HTTPException(status_code=409, detail="已有 AI 答案任务在运行，请稍候")
        with closing(connect()) as conn:
            card_ids = _draft_ids(conn, payload.module_id, payload.limit, payload.force)
        if not card_ids:
            raise HTTPException(status_code=400, detail="当前范围内没有需要生成答案的草稿")

        if len(_jobs) > 20:
            for job_id in [
                key for key, job in _jobs.items() if job["status"] not in _ACTIVE
            ][: len(_jobs) - 20]:
                _jobs.pop(job_id, None)

        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "phase": "排队中",
            "processed": 0,
            "total": len(card_ids),
            "report": None,
            "error": None,
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
        }

    threading.Thread(
        target=_run_batch,
        args=(job_id, settings, card_ids, payload.force),
        daemon=True,
    ).start()
    return {"job_id": job_id, "total": len(card_ids)}


@router.get("/ai-answer/jobs/{job_id}")
def get_batch_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="AI 答案任务不存在")
        return dict(job)
