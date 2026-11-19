"""采集流程：抓取 RSS → 清洗 → AI 切题 → 归并 → 自动推送。"""

import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field
from typing import Callable

from .. import config
from ..ai import AIClient, AIError
from ..anki import AnkiClient, push
from ..db import connect, utc_now
from .clean import clean_post
from .extract import extract_mentions
from .merge import Merger, ensure_module
from .rss import FeedError, fetch_feed

Progress = Callable[[str, int, int], None]

MAX_RETRY_POSTS = 20

_run_lock = threading.Lock()


def is_running() -> bool:
    return _run_lock.locked()


@dataclass
class FetchReport:
    feeds: int = 0
    items: int = 0
    new_posts: int = 0
    processed_posts: int = 0
    mentions: int = 0
    attached: int = 0
    created: int = 0
    candidates: int = 0
    ai_failed: int = 0
    embeddings: int = 0
    push: dict | None = None
    errors: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _notify(progress: Progress | None, phase: str, done: int, total: int) -> None:
    if progress:
        progress(phase, done, total)


def ensure_default_feed(settings: dict) -> None:
    url = (settings.get("rss_url") or "").strip()
    if not url:
        return
    interval = int(settings.get("fetch_interval_minutes") or 60)
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO feed(url, name, enabled, interval_minutes) VALUES(?, ?, 1, ?) "
            "ON CONFLICT(url) DO NOTHING",
            (url, "牛客面经", interval),
        )


def _start_log(conn, feed_id: int) -> int:
    cursor = conn.execute(
        "INSERT INTO fetch_log(feed_id, started_at) VALUES(?, ?)",
        (feed_id, utc_now()),
    )
    conn.commit()
    return cursor.lastrowid


def _finish_log(
    conn,
    log_id: int,
    items: int,
    new_posts: int,
    all_new: int,
    error: str | None,
) -> None:
    conn.execute(
        "UPDATE fetch_log SET finished_at=?, items=?, new_posts=?, all_new=?, error=? "
        "WHERE id=?",
        (utc_now(), items, new_posts, all_new, error, log_id),
    )
    conn.commit()


def _insert_post(conn, feed_url: str, item) -> int:
    cursor = conn.execute(
        "INSERT INTO post(guid, feed_id, title, author, url, raw_html, clean_md, "
        "fetched_at, ai_status) "
        "VALUES(?, (SELECT id FROM feed WHERE url=?), ?, ?, ?, ?, ?, ?, 'pending')",
        (
            item.guid,
            feed_url,
            item.title,
            item.author,
            item.link,
            item.description,
            clean_post(item.description),
            utc_now(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def _process_post(
    conn,
    merger: Merger,
    client: AIClient,
    post_id: int,
    report: FetchReport,
) -> None:
    post = conn.execute("SELECT * FROM post WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        return

    if not client.configured:
        conn.execute("UPDATE post SET ai_status='failed' WHERE id=?", (post_id,))
        conn.commit()
        report.ai_failed += 1
        report.errors.append(f"面经 {post_id}: 未配置 AI，无法切题")
        return

    module_paths = [
        row["path"] for row in conn.execute("SELECT path FROM module ORDER BY path")
    ]
    try:
        mentions = extract_mentions(client, dict(post), module_paths)
    except AIError as exc:
        conn.execute("UPDATE post SET ai_status='failed' WHERE id=?", (post_id,))
        conn.commit()
        report.ai_failed += 1
        report.errors.append(f"面经 {post_id}: {exc}")
        return

    conn.execute("DELETE FROM mention WHERE post_id = ?", (post_id,))
    report.processed_posts += 1

    for seq, item in enumerate(mentions, start=1):
        module_id = (
            ensure_module(conn, item["module"], "ai") if item["module"] else None
        )
        cursor = conn.execute(
            "INSERT INTO mention"
            "(post_id, seq, raw_text, context_md, section, topic, subtype, status, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                post_id,
                seq,
                item["question"],
                item["context"],
                item["section"],
                item["topic"],
                item["subtype"],
                utc_now(),
            ),
        )
        mention_id = cursor.lastrowid
        action, kp_id = merger.resolve(
            item["question"], module_id, mention_id, item["subtype"]
        )
        status = "pending" if action in ("candidate", "failed") else "attached"
        conn.execute(
            "UPDATE mention SET kp_id = ?, status = ? WHERE id = ?",
            (kp_id, status, mention_id),
        )
        report.mentions += 1
        if action == "attached":
            report.attached += 1
        elif action == "created":
            report.created += 1
        elif action == "candidate":
            report.candidates += 1

    conn.execute("UPDATE post SET ai_status='done' WHERE id=?", (post_id,))
    conn.commit()


def _auto_push(
    settings: dict, progress: Progress | None, report: FetchReport
) -> None:
    _notify(progress, "推送 Anki", 0, 1)
    try:
        client = AnkiClient(settings["anki_url"] or config.ANKI_CONNECT_URL)
        result = push(
            client,
            settings["anki_model"],
            settings["anki_sync_enabled"],
            progress,
        )
        report.push = {
            key: result[key]
            for key in ("changed", "added", "updated", "failed", "sync")
        }
    except Exception as exc:  # noqa: BLE001 - 推送失败不影响抓取结果
        report.errors.append(f"自动推送失败: {exc}")
    finally:
        _notify(progress, "推送 Anki", 1, 1)


def run_fetch(settings: dict, progress: Progress | None = None) -> dict:
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("已有采集任务在运行")
    try:
        return _run_fetch(settings, progress)
    finally:
        _run_lock.release()


def _run_fetch(settings: dict, progress: Progress | None = None) -> dict:
    started = time.time()
    report = FetchReport()
    client = AIClient(
        settings.get("ai_base_url", ""),
        settings.get("ai_api_key", ""),
        settings.get("ai_chat_model", ""),
        settings.get("ai_embedding_model", ""),
    )
    ensure_default_feed(settings)

    with closing(connect()) as conn:
        feeds = conn.execute(
            "SELECT id, url FROM feed WHERE enabled = 1 ORDER BY id"
        ).fetchall()
    report.feeds = len(feeds)

    with closing(connect()) as conn:
        merger = Merger(conn, client, settings)
        _notify(progress, "补全向量", 0, 1)
        report.embeddings = merger.backfill()
        _notify(progress, "补全向量", 1, 1)
        if merger.vector_error:
            report.errors.append(f"向量检索不可用: {merger.vector_error}")

        total = len(feeds)
        for index, feed in enumerate(feeds, start=1):
            _notify(progress, "抓取 RSS", index - 1, total)
            log_id = _start_log(conn, feed["id"])
            try:
                items = fetch_feed(feed["url"])
            except FeedError as exc:
                _finish_log(conn, log_id, 0, 0, 0, str(exc))
                report.errors.append(str(exc))
                continue

            report.items += len(items)
            new_posts = 0
            for seq, item in enumerate(items, start=1):
                _notify(progress, f"抓取 RSS · {index}/{total}", seq, len(items))
                if conn.execute(
                    "SELECT 1 FROM post WHERE guid = ?", (item.guid,)
                ).fetchone():
                    continue
                post_id = _insert_post(conn, feed["url"], item)
                new_posts += 1
                _notify(progress, "AI 切题", new_posts, len(items))
                _process_post(conn, merger, client, post_id, report)

            all_new = 1 if len(items) >= 6 and new_posts == len(items) else 0
            conn.execute(
                "UPDATE feed SET last_fetched_at = ? WHERE id = ?",
                (utc_now(), feed["id"]),
            )
            _finish_log(conn, log_id, len(items), new_posts, all_new, None)
            report.new_posts += new_posts
            _notify(progress, "抓取 RSS", index, total)

        pending = conn.execute(
            "SELECT id FROM post WHERE ai_status IN ('pending', 'failed') "
            "ORDER BY id LIMIT ?",
            (MAX_RETRY_POSTS,),
        ).fetchall()
        for index, row in enumerate(pending, start=1):
            _notify(progress, "重试切题", index, len(pending))
            _process_post(conn, merger, client, row["id"], report)

        if merger.vector_ready:
            _notify(progress, "补全向量", 0, 1)
            report.embeddings += merger.backfill()
            _notify(progress, "补全向量", 1, 1)

    _auto_push(settings, progress, report)
    report.elapsed_ms = int((time.time() - started) * 1000)
    return report.to_dict()
