"""审核队列：合并 / 保持独立 / 忽略，操作留痕与撤销，全库语义去重扫描。"""

import json
import math
import threading
import time
from contextlib import closing
from typing import Any, Callable

from .ai import AIClient, AIError
from .collect.merge import Merger, _verify_same
from .db import connect, utc_now

Progress = Callable[[str, int, int], None]

ACTIONS = {"merge", "keep", "ignore"}
SCAN_VERIFY_LIMIT = 60

_scan_lock = threading.Lock()


class ReviewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def is_scanning() -> bool:
    return _scan_lock.locked()


def _notify(progress: Progress | None, phase: str, done: int, total: int) -> None:
    if progress:
        progress(phase, done, total)


def _snapshot(conn, table: str, where: str, params: tuple) -> list[dict]:
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} {where}", params)]


def list_candidates(
    status: str = "pending",
    page: int = 1,
    page_size: int = 20,
    q: str | None = None,
) -> dict:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    conditions: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        conditions.append("c.status = ?")
        params.append(status)
    if q and q.strip():
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            "(lk.front_md LIKE ? ESCAPE '\\' OR rk.front_md LIKE ? ESCAPE '\\')"
        )
        params.extend([f"%{escaped}%", f"%{escaped}%"])
    where = " AND ".join(conditions) if conditions else "1 = 1"

    with closing(connect()) as conn:
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS value FROM dup_candidate c
            JOIN knowledge_point lk ON lk.id = c.left_kp_id
            JOIN knowledge_point rk ON rk.id = c.right_kp_id
            WHERE {where}
            """,
            params,
        ).fetchone()["value"]

        rows = conn.execute(
            f"""
            SELECT c.id, c.left_kp_id, c.right_kp_id, c.mention_id, c.score,
                   c.reason, c.status, c.created_at, c.resolved_at,
                   lk.front_md AS left_front, lk.back_md AS left_back,
                   lk.status AS left_status, lm.path AS left_module,
                   rk.front_md AS right_front, rk.back_md AS right_back,
                   rk.status AS right_status, rm.path AS right_module,
                   me.raw_text AS mention_text, me.section AS mention_section,
                   p.id AS post_id, p.title AS post_title
            FROM dup_candidate c
            JOIN knowledge_point lk ON lk.id = c.left_kp_id
            JOIN knowledge_point rk ON rk.id = c.right_kp_id
            LEFT JOIN module lm ON lm.id = lk.module_id
            LEFT JOIN module rm ON rm.id = rk.module_id
            LEFT JOIN mention me ON me.id = c.mention_id
            LEFT JOIN post p ON p.id = me.post_id
            WHERE {where}
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()

    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _restore(conn, table: str, row: dict, key: str) -> None:
    exists = conn.execute(
        f"SELECT 1 FROM {table} WHERE {key} = ?", (row[key],)
    ).fetchone()
    columns = [column for column in row if column != key]
    if exists:
        conn.execute(
            f"UPDATE {table} SET {', '.join(f'{column} = ?' for column in columns)} "
            f"WHERE {key} = ?",
            [row[column] for column in columns] + [row[key]],
        )
        return
    names = list(row)
    conn.execute(
        f"INSERT INTO {table}({', '.join(names)}) "
        f"VALUES({', '.join('?' for _ in names)})",
        [row[name] for name in names],
    )


def _resolve(conn, candidate, action: str) -> dict:
    left_id = candidate["left_kp_id"]
    right_id = candidate["right_kp_id"]
    mention_id = candidate["mention_id"]

    left = conn.execute(
        "SELECT * FROM knowledge_point WHERE id = ?", (left_id,)
    ).fetchone()
    right = conn.execute(
        "SELECT * FROM knowledge_point WHERE id = ?", (right_id,)
    ).fetchone()
    if left is None or right is None:
        raise ReviewError("候选关联的卡片已不存在", 409)

    if action == "merge":
        kps = [dict(left), dict(right)]
        mentions = _snapshot(conn, "mention", "WHERE kp_id = ?", (right_id,))
        syncs = _snapshot(
            conn, "anki_sync", "WHERE kp_id IN (?, ?)", (left_id, right_id)
        )
    elif action == "keep":
        kps = []
        mentions = (
            _snapshot(conn, "mention", "WHERE id = ?", (mention_id,))
            if mention_id is not None
            else []
        )
        syncs = []
    else:
        kps = [dict(right)]
        mentions = _snapshot(conn, "mention", "WHERE kp_id = ?", (right_id,))
        syncs = _snapshot(conn, "anki_sync", "WHERE kp_id = ?", (right_id,))

    now = utc_now()

    payload = {
        "action": action,
        "candidate": dict(candidate),
        "kps": kps,
        "mentions": mentions,
        "syncs": syncs,
        "summary": {
            "left_front": left["front_md"],
            "right_front": right["front_md"],
            "mention_text": mentions[0]["raw_text"] if mentions else None,
        },
    }
    cursor = conn.execute(
        "INSERT INTO review_action(candidate_id, action, payload, created_at) "
        "VALUES(?, ?, ?, ?)",
        (
            candidate["id"],
            action,
            json.dumps(payload, ensure_ascii=False),
            now,
        ),
    )
    action_id = cursor.lastrowid

    if action == "merge":
        merged_back = left["back_md"] or ""
        if right["back_md"] and right["back_md"].strip() != merged_back.strip():
            merged_back = (
                f"{merged_back.rstrip()}\n\n---\n\n{right['back_md'].strip()}"
                if merged_back.strip()
                else right["back_md"].strip()
            )

        conn.execute(
            "UPDATE mention SET kp_id = ?, status = 'attached' WHERE kp_id = ?",
            (left_id, right_id),
        )

        note_id = left["anki_note_id"]
        if note_id is None and right["anki_note_id"] is not None:
            note_id = right["anki_note_id"]
            left_sync = conn.execute(
                "SELECT 1 FROM anki_sync WHERE kp_id = ?", (left_id,)
            ).fetchone()
            if left_sync is None:
                right_sync = conn.execute(
                    "SELECT * FROM anki_sync WHERE kp_id = ?", (right_id,)
                ).fetchone()
                if right_sync is not None:
                    conn.execute("DELETE FROM anki_sync WHERE kp_id = ?", (right_id,))
                    conn.execute(
                        "INSERT INTO anki_sync"
                        "(kp_id, anki_note_id, pushed_hash, pushed_deck, pushed_at, last_error) "
                        "VALUES(?, ?, NULL, ?, ?, ?)",
                        (
                            left_id,
                            right_sync["anki_note_id"],
                            right_sync["pushed_deck"],
                            right_sync["pushed_at"],
                            right_sync["last_error"],
                        ),
                    )

        status = left["status"]
        if merged_back.strip() and status == "draft":
            status = "ready"

        conn.execute(
            "UPDATE knowledge_point SET back_md = ?, status = ?, anki_note_id = ?, "
            "updated_at = ? WHERE id = ?",
            (merged_back, status, note_id, now, left_id),
        )
        conn.execute(
            "UPDATE anki_sync SET pushed_hash = NULL WHERE kp_id = ?", (left_id,)
        )
        conn.execute("DELETE FROM knowledge_point WHERE id = ?", (right_id,))

    elif action == "keep":
        if mention_id is not None:
            conn.execute(
                "UPDATE mention SET status = 'attached' WHERE id = ?", (mention_id,)
            )
        conn.execute(
            "UPDATE dup_candidate SET status = 'kept', resolved_at = ? WHERE id = ?",
            (now, candidate["id"]),
        )

    else:  # ignore
        if mention_id is not None:
            conn.execute("DELETE FROM mention WHERE id = ?", (mention_id,))
            remaining = conn.execute(
                "SELECT COUNT(*) AS value FROM mention WHERE kp_id = ?", (right_id,)
            ).fetchone()["value"]
            empty_draft = not (right["back_md"] or "").strip()
            if remaining == 0 and empty_draft:
                conn.execute("DELETE FROM knowledge_point WHERE id = ?", (right_id,))
        if conn.execute(
            "SELECT 1 FROM dup_candidate WHERE id = ?", (candidate["id"],)
        ).fetchone():
            conn.execute(
                "UPDATE dup_candidate SET status = 'ignored', resolved_at = ? WHERE id = ?",
                (now, candidate["id"]),
            )

    return {"action_id": action_id}


def resolve_candidate(candidate_id: int, action: str) -> dict:
    if action not in ACTIONS:
        raise ReviewError("不支持的操作")
    with closing(connect()) as conn, conn:
        candidate = conn.execute(
            "SELECT * FROM dup_candidate WHERE id = ?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            raise ReviewError("候选不存在", 404)
        if candidate["status"] != "pending":
            raise ReviewError("该候选已处理")
        result = _resolve(conn, candidate, action)
    return {"candidate_id": candidate_id, "action": action, **result}


def resolve_batch(candidate_ids: list[int], action: str) -> dict:
    if action not in ACTIONS:
        raise ReviewError("不支持的操作")
    resolved: list[int] = []
    errors: list[dict] = []
    seen: set[int] = set()
    for candidate_id in candidate_ids:
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        try:
            resolve_candidate(candidate_id, action)
            resolved.append(candidate_id)
        except ReviewError as exc:
            errors.append({"id": candidate_id, "error": str(exc)})
    return {"resolved": resolved, "errors": errors}


def list_actions(limit: int = 30) -> dict:
    limit = min(100, max(1, limit))
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM review_action ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    items = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            payload = {}
        kps = payload.get("kps") or []
        candidate = payload.get("candidate") or {}
        mentions = payload.get("mentions") or []
        summary = payload.get("summary") or {}
        items.append(
            {
                "id": row["id"],
                "action": row["action"],
                "created_at": row["created_at"],
                "undone_at": row["undone_at"],
                "left_front": summary.get("left_front")
                or (kps[0]["front_md"] if kps else None),
                "right_front": summary.get("right_front")
                or (kps[1]["front_md"] if len(kps) > 1 else None),
                "mention_text": summary.get("mention_text")
                or (mentions[0]["raw_text"] if mentions else None),
                "candidate_id": candidate.get("id"),
            }
        )
    return {"items": items}


def undo_action(action_id: int) -> dict:
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT * FROM review_action WHERE id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise ReviewError("操作记录不存在", 404)
        if row["undone_at"] is not None:
            raise ReviewError("该操作已撤销")

        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError as exc:
            raise ReviewError("操作快照损坏，无法撤销") from exc

        for snapshot in payload.get("kps") or []:
            _restore(conn, "knowledge_point", snapshot, "id")
        for snapshot in payload.get("mentions") or []:
            _restore(conn, "mention", snapshot, "id")
        for snapshot in payload.get("syncs") or []:
            _restore(conn, "anki_sync", snapshot, "kp_id")

        candidate = payload.get("candidate")
        if candidate:
            candidate = dict(candidate)
            candidate["status"] = "pending"
            candidate["resolved_at"] = None
            _restore(conn, "dup_candidate", candidate, "id")

        conn.execute(
            "UPDATE review_action SET undone_at = ? WHERE id = ?",
            (utc_now(), action_id),
        )
    return {"action_id": action_id, "undone": True}


def scan(settings: dict, progress: Progress | None = None) -> dict:
    if not _scan_lock.acquire(blocking=False):
        raise ReviewError("已有全库扫描任务在运行", 409)
    try:
        return _scan(settings, progress)
    finally:
        _scan_lock.release()


def _scan(settings: dict, progress: Progress | None = None) -> dict:
    started = time.time()
    client = AIClient(
        settings.get("ai_base_url", ""),
        settings.get("ai_api_key", ""),
        settings.get("ai_chat_model", ""),
        settings.get("ai_embedding_model", ""),
        settings.get("ai_embedding_base_url", ""),
        settings.get("ai_embedding_api_key", ""),
    )
    if not client.embeddings_configured:
        raise ReviewError("未配置 Embedding 模型，无法执行全库扫描")
    low = min(
        float(settings["dup_high_threshold"]),
        float(settings["dup_low_threshold"]),
    )

    errors: list[str] = []
    reviewed = 0
    inserted = 0
    above = 0
    pairs = 0

    with closing(connect()) as conn:
        merger = Merger(conn, client, settings)
        if not merger.vector_ready:
            raise ReviewError(merger.vector_error or "向量检索不可用")

        _notify(progress, "补全向量", 0, 1)
        embeddings = merger.backfill()
        _notify(progress, "补全向量", 1, 1)
        if not merger.vector_ready:
            raise ReviewError(merger.vector_error or "向量检索不可用")

        vectors = merger.store.load_all()
        rows = conn.execute(
            "SELECT id, front_md, module_id, status FROM knowledge_point "
            "WHERE status != 'archived' ORDER BY id"
        ).fetchall()
        items = [
            (row["id"], vectors[row["id"]])
            for row in rows
            if row["id"] in vectors
        ]
        fronts = {row["id"]: row["front_md"] for row in rows}

        norms = {kp_id: math.sqrt(sum(value * value for value in vector)) for kp_id, vector in items}
        scored: list[tuple[float, int, int]] = []
        total_rows = len(items)
        total_pairs = total_rows * (total_rows - 1) // 2
        for index, (left_id, left_vector) in enumerate(items):
            left_norm = norms[left_id]
            if left_norm == 0:
                continue
            for right_id, right_vector in items[index + 1 :]:
                right_norm = norms[right_id]
                if right_norm == 0:
                    continue
                dot = sum(a * b for a, b in zip(left_vector, right_vector))
                score = dot / (left_norm * right_norm)
                if score >= low:
                    scored.append((score, left_id, right_id))
            if index % 20 == 0:
                _notify(progress, "两两比对", index, total_rows)

        pairs = total_pairs
        above = len(scored)
        scored.sort(key=lambda item: item[0], reverse=True)
        _notify(progress, "两两比对", total_rows, total_rows)

        existing = {
            frozenset((row["left_kp_id"], row["right_kp_id"]))
            for row in conn.execute(
                "SELECT left_kp_id, right_kp_id FROM dup_candidate "
                "WHERE status = 'pending'"
            )
        }
        shortlist = [
            item
            for item in scored
            if frozenset((item[1], item[2])) not in existing
        ][:SCAN_VERIFY_LIMIT]

        for index, (score, left_id, right_id) in enumerate(shortlist, start=1):
            _notify(progress, "LLM 复核", index - 1, len(shortlist))
            try:
                same, reason = _verify_same(
                    client, fronts[left_id], fronts[right_id]
                )
            except AIError as exc:
                errors.append(f"{left_id}-{right_id}: {exc}")
                continue
            reviewed += 1
            if not same:
                continue
            conn.execute(
                "INSERT INTO dup_candidate"
                "(left_kp_id, right_kp_id, mention_id, score, reason, created_at) "
                "VALUES(?, ?, NULL, ?, ?, ?)",
                (
                    left_id,
                    right_id,
                    score,
                    reason or f"相似度 {score:.2f}",
                    utc_now(),
                ),
            )
            conn.commit()
            inserted += 1
        _notify(progress, "LLM 复核", len(shortlist), len(shortlist))

    return {
        "knowledge_points": total_rows,
        "pairs": pairs,
        "above_threshold": above,
        "reviewed": reviewed,
        "candidates": inserted,
        "embeddings": embeddings,
        "errors": errors,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
