"""审核队列接口：候选查询、合并/保持/忽略、全库扫描与撤销。"""

import threading
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import review
from ..db import utc_now
from .settings import load_settings

router = APIRouter(prefix="/api", tags=["review"])

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_ACTIVE = ("queued", "running")


class ResolveIn(BaseModel):
    action: str
    keep_side: str = "left"


class BatchIn(BaseModel):
    ids: list[int] = Field(min_length=1)
    action: str
    keep_side: str = "left"


@router.get("/dup-candidates")
def list_candidates(
    status: str = "pending",
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    return review.list_candidates(status, page, page_size, q)


@router.post("/dup-candidates/{candidate_id}/resolve")
def resolve_candidate(candidate_id: int, payload: ResolveIn) -> dict:
    try:
        return review.resolve_candidate(
            candidate_id, payload.action, payload.keep_side
        )
    except review.ReviewError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("/dup-candidates/batch")
def resolve_batch(payload: BatchIn) -> dict:
    try:
        return review.resolve_batch(payload.ids, payload.action, payload.keep_side)
    except review.ReviewError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _set(job_id: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _run_scan(job_id: str, settings: dict) -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _set(job_id, phase=phase, processed=done, total=total)

    _set(job_id, status="running", started_at=utc_now())
    try:
        report = review.scan(settings, progress)
        _set(job_id, status="done", report=report, finished_at=utc_now())
    except Exception as exc:  # noqa: BLE001
        _set(job_id, status="failed", error=str(exc), finished_at=utc_now())


@router.post("/dedupe/scan")
def start_scan() -> dict:
    with _lock:
        if any(job["status"] in _ACTIVE for job in _jobs.values()) or review.is_scanning():
            raise HTTPException(status_code=409, detail="已有全库扫描任务在运行，请稍候")
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
            "total": 0,
            "report": None,
            "error": None,
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
        }

    settings = load_settings()
    threading.Thread(target=_run_scan, args=(job_id, settings), daemon=True).start()
    return {"job_id": job_id}


@router.get("/dedupe/jobs/{job_id}")
def get_scan_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="扫描任务不存在")
        return dict(job)


@router.get("/review/actions")
def list_actions(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
) -> dict:
    return review.list_actions(page, page_size)


@router.post("/review/actions/{action_id}/undo")
def undo_action(action_id: int) -> dict:
    try:
        return review.undo_action(action_id)
    except review.ReviewError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
