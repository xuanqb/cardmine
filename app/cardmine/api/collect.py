import sqlite3
import threading
import uuid
from contextlib import closing
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import groups
from ..collect import is_running, run_fetch
from ..db import connect, utc_now
from .settings import load_settings

router = APIRouter(prefix="/api", tags=["collect"])

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_ACTIVE = ("queued", "running")


class FeedIn(BaseModel):
    url: str
    name: str | None = None
    enabled: bool = True
    interval_minutes: int = Field(60, ge=5)
    group_id: int | None = None


class FeedPatch(BaseModel):
    url: str | None = None
    name: str | None = None
    enabled: bool | None = None
    interval_minutes: int | None = Field(None, ge=5)
    group_id: int | None = None


def _feed_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": row["url"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "interval_minutes": row["interval_minutes"],
        "last_fetched_at": row["last_fetched_at"],
        "group_id": row["group_id"],
        "group_name": row["group_name"],
        "group_path": row["group_path"],
    }


FEED_SELECT = (
    "SELECT f.*, m.name AS group_name, m.path AS group_path, "
    "(SELECT COUNT(*) FROM post p WHERE p.feed_id = f.id) AS posts, "
    "(SELECT COUNT(*) FROM mention me JOIN post p ON p.id = me.post_id "
    " WHERE p.feed_id = f.id) AS mentions "
    "FROM feed f LEFT JOIN module m ON m.id = f.group_id "
)


def _check_group(conn, group_id: int | None) -> None:
    if group_id is not None and groups.get_group(conn, group_id) is None:
        raise HTTPException(status_code=400, detail="目标组必须是顶层模块")


@router.get("/feeds")
def list_feeds() -> dict:
    with closing(connect()) as conn:
        rows = conn.execute(
            f"{FEED_SELECT} WHERE f.url NOT LIKE 'manual://%' ORDER BY f.id"
        ).fetchall()
    return {
        "items": [
            {**_feed_row(row), "posts": row["posts"], "mentions": row["mentions"]}
            for row in rows
        ]
    }


@router.post("/feeds")
def create_feed(payload: FeedIn) -> dict:
    if not payload.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="RSS 地址必须以 http(s):// 开头")
    with closing(connect()) as conn, conn:
        _check_group(conn, payload.group_id)
        try:
            cursor = conn.execute(
                "INSERT INTO feed(url, name, enabled, interval_minutes, group_id) "
                "VALUES(?,?,?,?,?)",
                (
                    payload.url.strip(),
                    payload.name,
                    int(payload.enabled),
                    payload.interval_minutes,
                    payload.group_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="该 RSS 源已存在") from exc
        feed_id = cursor.lastrowid
    return {"id": feed_id}


@router.patch("/feeds/{feed_id}")
def update_feed(feed_id: int, payload: FeedPatch) -> dict:
    updates = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None or key == "group_id"
    }
    if "url" in updates and not updates["url"].startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="RSS 地址必须以 http(s):// 开头")
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])

    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT id FROM feed WHERE id = ?", (feed_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="RSS 源不存在")
        _check_group(conn, updates.get("group_id"))
        if updates:
            columns = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE feed SET {columns} WHERE id = ?",
                (*updates.values(), feed_id),
            )
        result = conn.execute(f"{FEED_SELECT} WHERE f.id = ?", (feed_id,)).fetchone()
    return _feed_row(result)


@router.delete("/feeds/{feed_id}")
def delete_feed(feed_id: int) -> dict:
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT id FROM feed WHERE id = ?", (feed_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="RSS 源不存在")
        posts = conn.execute(
            "SELECT COUNT(*) AS value FROM post WHERE feed_id = ?", (feed_id,)
        ).fetchone()["value"]
        if posts:
            raise HTTPException(
                status_code=409, detail="该源已有面经，不能删除；可先停用"
            )
        conn.execute("DELETE FROM feed WHERE id = ?", (feed_id,))
    return {"deleted": feed_id}


def _set(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _run_job(job_id: str, settings: dict) -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _set(job_id, phase=phase, processed=done, total=total)

    _set(job_id, status="running", started_at=utc_now())
    try:
        report = run_fetch(settings, progress)
        _set(job_id, status="done", report=report, finished_at=utc_now())
    except Exception as exc:  # noqa: BLE001
        _set(job_id, status="failed", error=str(exc), finished_at=utc_now())


@router.post("/fetch/run")
def start_fetch() -> dict:
    with _lock:
        if any(job["status"] in _ACTIVE for job in _jobs.values()) or is_running():
            raise HTTPException(status_code=409, detail="已有采集任务在运行，请稍候")
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
    threading.Thread(target=_run_job, args=(job_id, settings), daemon=True).start()
    return {"job_id": job_id}


@router.get("/fetch/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="采集任务不存在")
        return dict(job)


@router.get("/posts")
def list_posts(
    page: int = 1,
    page_size: int = 20,
    feed_id: int | None = None,
    ai_status: str | None = None,
    q: str | None = None,
) -> dict:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    conditions = []
    params: list[Any] = []
    if feed_id is not None:
        conditions.append("p.feed_id = ?")
        params.append(feed_id)
    if ai_status:
        conditions.append("p.ai_status = ?")
        params.append(ai_status)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append("p.title LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with closing(connect()) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS value FROM post p {where}", params
        ).fetchone()["value"]
        rows = conn.execute(
            f"""
            SELECT p.id, p.title, p.author, p.url, p.fetched_at, p.ai_status,
                   f.name AS feed_name,
                   (SELECT COUNT(*) FROM mention m WHERE m.post_id = p.id) AS mentions
            FROM post p JOIN feed f ON f.id = p.feed_id
            {where}
            ORDER BY p.id DESC LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()

    return {
        "items": [
            {
                "id": row["id"],
                "title": row["title"],
                "author": row["author"],
                "url": row["url"],
                "fetched_at": row["fetched_at"],
                "ai_status": row["ai_status"],
                "feed_name": row["feed_name"],
                "mentions": row["mentions"],
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/posts/{post_id}")
def get_post(post_id: int) -> dict:
    with closing(connect()) as conn:
        post = conn.execute(
            """
            SELECT p.*, f.name AS feed_name
            FROM post p JOIN feed f ON f.id = p.feed_id
            WHERE p.id = ?
            """,
            (post_id,),
        ).fetchone()
        if post is None:
            raise HTTPException(status_code=404, detail="面经不存在")
        mentions = conn.execute(
            """
            SELECT m.id, m.seq, m.raw_text, m.context_md, m.section, m.topic,
                   m.subtype, m.status, kp.id AS kp_id, kp.front_md AS kp_front,
                   kp.status AS kp_status
            FROM mention m LEFT JOIN knowledge_point kp ON kp.id = m.kp_id
            WHERE m.post_id = ? ORDER BY m.seq
            """,
            (post_id,),
        ).fetchall()

    return {
        "id": post["id"],
        "title": post["title"],
        "author": post["author"],
        "url": post["url"],
        "feed_name": post["feed_name"],
        "fetched_at": post["fetched_at"],
        "ai_status": post["ai_status"],
        "clean_md": post["clean_md"],
        "mentions": [dict(row) for row in mentions],
    }
