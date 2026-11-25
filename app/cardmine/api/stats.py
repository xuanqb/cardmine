import time
import re
from contextlib import closing
from datetime import datetime, timezone

from fastapi import APIRouter

from .. import config
from ..anki import AnkiClient, AnkiError, targets
from ..anki.service import summary as anki_summary
from ..collect import is_running
from ..db import connect
from ..export import local_media_refs
from .settings import load_settings

router = APIRouter(prefix="/api/stats", tags=["stats"])

_anki_cache: dict = {"at": 0.0, "value": None}
_ANKI_TTL = 15


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _elapsed_ms(started: str | None, finished: str | None) -> int | None:
    start = _parse_time(started)
    end = _parse_time(finished)
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _anki_status(settings: dict) -> dict:
    now = time.monotonic()
    cached = _anki_cache["value"]
    if cached is not None and now - _anki_cache["at"] < _ANKI_TTL:
        return cached

    with closing(connect()) as conn:
        rows = targets.list_targets(conn, enabled_only=True)
    status = {
        "url": rows[0]["base_url"] if rows else None,
        "model": settings["anki_model"],
        "connected": False,
        "version": None,
        "error": "未配置启用的同步目标" if not rows else None,
    }
    if rows:
        client = AnkiClient(
            rows[0]["base_url"],
            conflict_guard=bool(settings.get("anki_conflict_guard", True)),
        )
        try:
            info = client.validate(settings["anki_model"], timeout=2)
            status.update(
                connected=True, version=info["version"], model=info["model"]
            )
        except (AnkiError, OSError, ValueError) as exc:
            status["error"] = str(exc)

    _anki_cache["at"] = now
    _anki_cache["value"] = status
    return status


def _fetch_status(conn) -> dict:
    feeds = conn.execute(
        """
        SELECT f.id, f.name, f.url, f.interval_minutes, f.last_fetched_at,
               (SELECT fl.id FROM fetch_log fl WHERE fl.feed_id = f.id
                ORDER BY fl.id DESC LIMIT 1) AS latest_log_id
        FROM feed f
        WHERE f.url NOT LIKE 'manual://%'
        ORDER BY f.id
        """
    ).fetchall()

    log_ids = [row["latest_log_id"] for row in feeds if row["latest_log_id"]]
    logs: dict[int, dict] = {}
    if log_ids:
        placeholders = ",".join("?" for _ in log_ids)
        for row in conn.execute(
            f"SELECT * FROM fetch_log WHERE id IN ({placeholders})", log_ids
        ):
            logs[row["id"]] = dict(row)

    recent = [
        dict(row)
        for row in conn.execute(
            """
            SELECT fl.id, fl.feed_id, fl.started_at, fl.finished_at, fl.items,
                   fl.new_posts, fl.all_new, fl.error, f.name AS feed_name
            FROM fetch_log fl JOIN feed f ON f.id = fl.feed_id
            ORDER BY fl.id DESC LIMIT 8
            """
        )
    ]
    for row in recent:
        row["elapsed_ms"] = _elapsed_ms(row["started_at"], row["finished_at"])

    now = datetime.now(timezone.utc)
    items = []
    for feed in feeds:
        latest = logs.get(feed["latest_log_id"])
        interval = max(5, int(feed["interval_minutes"] or 60))
        last = _parse_time(feed["last_fetched_at"])
        stale = last is None or (now - last).total_seconds() > interval * 120
        items.append(
            {
                "id": feed["id"],
                "name": feed["name"],
                "url": feed["url"],
                "interval_minutes": interval,
                "last_fetched_at": feed["last_fetched_at"],
                "stale": stale,
                "latest": latest,
            }
        )

    last_success = conn.execute(
        "SELECT MAX(finished_at) AS value FROM fetch_log "
        "WHERE finished_at IS NOT NULL AND error IS NULL"
    ).fetchone()["value"]

    return {
        "running": is_running(),
        "feeds": items,
        "recent": recent,
        "last_success_at": last_success,
    }


def _front_preview(text: str | None) -> str:
    plain = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", plain).strip()[:120]


def _media_status(conn) -> dict:
    rows = conn.execute(
        "SELECT k.id, k.front_md, k.back_md, m.path AS module_path "
        "FROM knowledge_point k LEFT JOIN module m ON m.id = k.module_id "
        "ORDER BY k.id"
    ).fetchall()

    refs: set[str] = set()
    cards: list[tuple] = []
    for row in rows:
        front_refs = local_media_refs(row["front_md"])
        back_refs = local_media_refs(row["back_md"])
        names = front_refs | back_refs
        if not names:
            continue
        refs |= names
        cards.append((row, front_refs, back_refs, names))

    missing = sorted(
        name for name in refs if not (config.MEDIA_DIR / name).is_file()
    )
    missing_set = set(missing)
    details: dict[str, dict] = {
        name: {"name": name, "card_count": 0, "cards": []} for name in missing
    }
    for row, front_refs, back_refs, names in cards:
        for name in names & missing_set:
            item = details[name]
            item["card_count"] += 1
            if len(item["cards"]) >= 5:
                continue
            item["cards"].append(
                {
                    "id": row["id"],
                    "front": _front_preview(row["front_md"]),
                    "module": row["module_path"] or "",
                    "where": [
                        label
                        for label, values in (
                            ("front", front_refs),
                            ("back", back_refs),
                        )
                        if name in values
                    ],
                }
            )

    total = conn.execute("SELECT COUNT(*) AS value FROM media").fetchone()["value"]
    return {
        "referenced": len(refs),
        "stored": total,
        "missing_count": len(missing),
        "missing": missing,
        "missing_items": [details[name] for name in missing],
        "sample": missing[:20],
    }


def _alerts(fetch: dict, cards: dict, review: dict, sync: dict, ai: dict, media: dict) -> list[dict]:
    alerts: list[dict] = []

    for feed in fetch["feeds"]:
        if feed["stale"]:
            alerts.append(
                {
                    "level": "warning",
                    "text": f"{feed['name'] or feed['url']} 已超过两个抓取周期未成功抓取",
                }
            )

    for log in fetch["recent"]:
        if log["error"]:
            alerts.append(
                {
                    "level": "danger",
                    "text": f"抓取失败（{log['feed_name']}，{log['started_at']}）：{log['error']}",
                }
            )
            break

    for log in fetch["recent"]:
        if log["all_new"]:
            alerts.append(
                {
                    "level": "warning",
                    "text": (
                        f"抓取窗口疑似整体滚动，可能漏抓：{log['feed_name']} "
                        f"在 {log['started_at']} 抓到的 {log['items']} 条全部为新条目"
                    ),
                }
            )
            break

    if review["pending_candidates"]:
        alerts.append(
            {
                "level": "info",
                "text": f"有 {review['pending_candidates']} 条待确认重复，请前往审核页处理",
            }
        )
    if sync["failed"]:
        alerts.append(
            {"level": "warning", "text": f"{sync['failed']} 张卡片上次推送失败"}
        )
    if sync.get("conflicts"):
        alerts.append(
            {
                "level": "warning",
                "text": f"有 {sync['conflicts']} 条 Anki 同步冲突待处理",
            }
        )
    if ai["failed_posts"]:
        alerts.append(
            {"level": "warning", "text": f"{ai['failed_posts']} 篇面经 AI 切题失败"}
        )
    if media["missing_count"]:
        alerts.append(
            {
                "level": "warning",
                "text": f"{media['missing_count']} 个被引用的媒体文件缺失",
                "items": media.get("missing_items") or [],
            }
        )
    if not cards["draft"] and not cards["ready"] and not cards["archived"]:
        alerts.append(
            {"level": "info", "text": "卡片库为空，可先导入 apkg 或前往面经页抓取"}
        )
    return alerts


@router.get("/dashboard")
def dashboard() -> dict:
    settings = load_settings()

    with closing(connect()) as conn:
        fetch = _fetch_status(conn)
        review = {
            "pending_candidates": conn.execute(
                "SELECT COUNT(*) AS value FROM dup_candidate WHERE status = 'pending'"
            ).fetchone()["value"]
        }
        ai_rows = conn.execute(
            "SELECT id, title, fetched_at FROM post WHERE ai_status = 'failed' "
            "ORDER BY id DESC LIMIT 10"
        ).fetchall()
        ai = {
            "failed_posts": conn.execute(
                "SELECT COUNT(*) AS value FROM post WHERE ai_status = 'failed'"
            ).fetchone()["value"],
            "recent": [dict(row) for row in ai_rows],
        }
        sync = {
            "failed": conn.execute(
                "SELECT COUNT(*) AS value FROM anki_sync WHERE last_error IS NOT NULL"
            ).fetchone()["value"],
            "conflicts": conn.execute(
                "SELECT COUNT(*) AS value FROM sync_conflict WHERE status = 'pending'"
            ).fetchone()["value"],
            "failed_items": [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT s.kp_id, s.last_error, kp.front_md
                    FROM anki_sync s JOIN knowledge_point kp ON kp.id = s.kp_id
                    WHERE s.last_error IS NOT NULL ORDER BY s.kp_id LIMIT 10
                    """
                )
            ],
        }
        media = _media_status(conn)

    summary = anki_summary()
    cards = {
        "draft": summary["cards"]["draft"],
        "ready": summary["cards"]["ready"],
        "archived": summary["cards"]["archived"],
        "total": sum(
            summary["cards"][key] for key in ("draft", "ready", "archived")
        ),
        "pending_push": summary["cards"]["pending_push"],
        "matched": summary["cards"]["matched"],
    }
    sync["pending_push"] = cards["pending_push"]
    sync["last_push_at"] = summary["last_push_at"]

    anki = _anki_status(settings)

    return {
        "fetch": fetch,
        "cards": cards,
        "review": review,
        "sync": sync,
        "anki": anki,
        "ai": ai,
        "media": media,
        "alerts": _alerts(fetch, cards, review, sync, ai, media),
    }
