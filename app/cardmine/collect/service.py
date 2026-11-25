"""采集流程：抓取 RSS → 清洗 → AI 切题 → 归并 → 自动推送。"""

import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, field
from typing import Callable

from .. import groups
from ..ai import AIClient, AIError
from ..anki import push_targets
from ..db import connect, utc_now
from .clean import clean_post
from .extract import MAX_INPUT_CHARS, extract_mentions
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
    progress: Progress | None = None,
) -> list[dict]:
    post = conn.execute(
        "SELECT p.*, f.group_id AS feed_group_id "
        "FROM post p JOIN feed f ON f.id = p.feed_id WHERE p.id = ?",
        (post_id,),
    ).fetchone()
    if post is None:
        return []

    group_id = post["feed_group_id"]
    group = groups.get_group(conn, group_id)

    if not client.configured:
        conn.execute("UPDATE post SET ai_status='failed' WHERE id=?", (post_id,))
        conn.commit()
        report.ai_failed += 1
        report.errors.append(f"面经 {post_id}: 未配置 AI，无法切题")
        return []

    module_paths = groups.subtree_paths(conn, group_id) if group is not None else [
        row["path"] for row in conn.execute("SELECT path FROM module ORDER BY path")
    ]
    _notify(progress, "AI 切题", 0, 1)
    try:
        mentions = extract_mentions(
            client,
            dict(post),
            module_paths,
            group["path"] if group is not None else None,
        )
    except AIError as exc:
        conn.execute("UPDATE post SET ai_status='failed' WHERE id=?", (post_id,))
        conn.commit()
        report.ai_failed += 1
        report.errors.append(f"面经 {post_id}: {exc}")
        return []
    _notify(progress, "AI 切题", 1, 1)

    conn.execute("DELETE FROM mention WHERE post_id = ?", (post_id,))
    report.processed_posts += 1
    _notify(progress, "归并题目", 0, 1)
    items = _store_mentions(conn, merger, post_id, group, group_id, mentions, report)
    _notify(progress, "归并题目", 1, 1)
    conn.execute("UPDATE post SET ai_status='done' WHERE id=?", (post_id,))
    conn.commit()
    return items


def _store_mentions(
    conn,
    merger: Merger,
    post_id: int,
    group,
    group_id: int | None,
    mentions: list[dict],
    report: FetchReport,
) -> list[dict]:
    items: list[dict] = []
    for seq, item in enumerate(mentions, start=1):
        module_id = (
            ensure_module(conn, item["module"], "ai", group) if item["module"] else None
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
            item["question"], module_id, mention_id, item["subtype"], group_id
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
        items.append(
            {
                "question": item["question"],
                "module": item["module"],
                "action": action,
                "kp_id": kp_id,
                "kp_front": _kp_front(conn, kp_id),
            }
        )
    return items


def _kp_front(conn, kp_id: int | None) -> str:
    if kp_id is None:
        return ""
    row = conn.execute(
        "SELECT front_md FROM knowledge_point WHERE id = ?", (kp_id,)
    ).fetchone()
    return row["front_md"] if row is not None else ""


def _manual_client(settings: dict) -> AIClient:
    return AIClient(
        settings.get("ai_base_url", ""),
        settings.get("ai_api_key", ""),
        settings.get("ai_chat_model", ""),
        settings.get("ai_embedding_model", ""),
        settings.get("ai_embedding_base_url", ""),
        settings.get("ai_embedding_api_key", ""),
    )


def _manual_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("面经正文不能为空")
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"正文超过 {MAX_INPUT_CHARS} 字，请拆分后分次导入")
    return text


def _manual_group(conn, group_id: int):
    group = groups.get_group(conn, group_id)
    if group is None:
        raise ValueError("请选择有效的顶层组")
    return group


def _manual_title(title: str | None, text: str) -> str:
    title = (title or "").strip()
    if title:
        return title[:200]
    for line in text.splitlines():
        candidate = line.strip().lstrip("#").strip()
        if candidate and set(candidate) - {"-", "="}:
            return candidate[:200]
    return f"手动导入 {utc_now()[:10]}"


def _manual_feed(conn, group) -> int:
    url = f"manual://group/{group['id']}"
    row = conn.execute("SELECT id FROM feed WHERE url = ?", (url,)).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO feed(url, name, enabled, interval_minutes, group_id) "
        "VALUES(?, ?, 0, 60, ?)",
        (url, f"手动导入 · {group['name']}", group["id"]),
    )
    return int(cursor.lastrowid)


def prepare_manual_post(settings: dict, title: str | None, text: str, group_id: int) -> dict:
    """入库前的同步校验，错误直接返回给调用方，不写库。"""
    text = _manual_text(text)
    with closing(connect()) as conn:
        _manual_group(conn, group_id)
    if not _manual_client(settings).configured:
        raise ValueError("未配置 AI，无法切题")
    return {"title": _manual_title(title, text), "text": text}


def run_manual_post(
    settings: dict,
    title: str | None,
    text: str,
    group_id: int,
    progress: Progress | None = None,
) -> dict:
    """把整篇面经当成一个 post 走采集链路：切题、归并、计入出现次数。"""
    started = time.time()
    prepared = prepare_manual_post(settings, title, text, group_id)
    text = prepared["text"]
    if not _run_lock.acquire(blocking=False):
        raise ValueError("采集或导入任务正在运行，请稍后重试")

    try:
        with closing(connect()) as conn:
            group = _manual_group(conn, group_id)
            feed_id = _manual_feed(conn, group)
            cursor = conn.execute(
                "INSERT INTO post"
                "(guid, feed_id, title, author, url, raw_html, clean_md, fetched_at, ai_status) "
                "VALUES(?, ?, ?, '手动导入', NULL, ?, ?, ?, 'pending')",
                (
                    f"manual-{uuid.uuid4().hex}",
                    feed_id,
                    prepared["title"],
                    text,
                    text,
                    utc_now(),
                ),
            )
            post_id = int(cursor.lastrowid)
            conn.commit()

            client = _manual_client(settings)
            merger = Merger(conn, client, settings)
            report = FetchReport()

            _notify(progress, "补全向量", 0, 1)
            if merger.vector_ready:
                report.embeddings += merger.backfill()
                if merger.vector_error:
                    report.errors.append(f"向量检索不可用: {merger.vector_error}")
            _notify(progress, "补全向量", 1, 1)

            items = _process_post(conn, merger, client, post_id, report, progress)
            conn.commit()
    finally:
        _run_lock.release()

    result = report.to_dict()
    result["post_id"] = post_id
    result["title"] = prepared["title"]
    result["items"] = items
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def delete_manual_post(post_id: int) -> dict:
    """删除手动导入的面经及其独占的未作答卡片。"""
    if not _run_lock.acquire(blocking=False):
        raise ValueError("采集或导入任务正在运行，请稍后重试")

    removed = 0
    try:
        with closing(connect()) as conn:
            post = conn.execute(
                "SELECT p.id, f.url AS feed_url, f.group_id AS feed_group_id "
                "FROM post p JOIN feed f ON f.id = p.feed_id WHERE p.id = ?",
                (post_id,),
            ).fetchone()
            if post is None:
                raise ValueError("面经不存在")
            if not (post["feed_url"] or "").startswith("manual://"):
                raise ValueError("只能删除手动导入的面经")

            kp_ids = [
                int(row["kp_id"])
                for row in conn.execute(
                    "SELECT DISTINCT kp_id FROM mention "
                    "WHERE post_id = ? AND kp_id IS NOT NULL",
                    (post_id,),
                )
            ]
            conn.execute("DELETE FROM mention WHERE post_id = ?", (post_id,))
            conn.execute("DELETE FROM post WHERE id = ?", (post_id,))

            modules: set[int] = set()
            for kp_id in kp_ids:
                if conn.execute(
                    "SELECT 1 FROM mention WHERE kp_id = ?", (kp_id,)
                ).fetchone():
                    continue
                row = conn.execute(
                    "SELECT module_id, status FROM knowledge_point WHERE id = ?",
                    (kp_id,),
                ).fetchone()
                if row is None or row["status"] != "draft":
                    continue
                if row["module_id"]:
                    modules.add(int(row["module_id"]))
                _delete_vectors(conn, kp_id)
                conn.execute("DELETE FROM knowledge_point WHERE id = ?", (kp_id,))
                removed += 1

            for module_id in sorted(modules, reverse=True):
                if conn.execute(
                    "SELECT 1 FROM module WHERE parent_id = ?", (module_id,)
                ).fetchone():
                    continue
                if conn.execute(
                    "SELECT 1 FROM knowledge_point WHERE module_id = ?", (module_id,)
                ).fetchone():
                    continue
                if conn.execute(
                    "SELECT 1 FROM kp_alias WHERE module_id = ?", (module_id,)
                ).fetchone():
                    continue
                conn.execute("DELETE FROM module WHERE id = ?", (module_id,))
            conn.commit()
    finally:
        _run_lock.release()

    return {"post_id": post_id, "removed_cards": removed}


def _delete_vectors(conn, kp_id: int) -> None:
    conn.execute("DELETE FROM kp_embedding WHERE kp_id = ?", (kp_id,))
    try:
        conn.execute("DELETE FROM vec_kp WHERE kp_id = ?", (kp_id,))
    except sqlite3.OperationalError:
        # 未启用 sqlite-vec 时没有 vec_kp 表
        pass


def _auto_push(
    settings: dict, progress: Progress | None, report: FetchReport
) -> None:
    _notify(progress, "推送 Anki", 0, 1)
    try:
        result = push_targets(
            settings["anki_model"],
            settings["anki_sync_enabled"],
            settings.get("anki_conflict_guard", True),
            progress,
        )
        report.push = {
            key: result.get(key, 0)
            for key in ("changed", "added", "updated", "failed")
        }
        report.push["sync"] = result.get("sync", "skipped")
        if result.get("errors"):
            report.errors.extend(result["errors"][:5])
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
        settings.get("ai_embedding_base_url", ""),
        settings.get("ai_embedding_api_key", ""),
    )

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
