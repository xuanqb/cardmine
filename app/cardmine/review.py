"""审核队列：合并 / 保持独立 / 忽略，操作留痕与撤销，全库语义去重扫描。"""

import json
import sqlite3
import threading
import time
from contextlib import closing
from typing import Any, Callable

from . import aliases
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


def _pair_key(left_id: int, right_id: int) -> tuple[int, int]:
    return (left_id, right_id) if left_id < right_id else (right_id, left_id)


def _record_exclusion(conn, left, right, action_id: int, now: str) -> None:
    left_id, right_id = _pair_key(int(left["id"]), int(right["id"]))
    first, second = (
        (left, right) if int(left["id"]) == left_id else (right, left)
    )
    conn.execute(
        "INSERT INTO dup_exclusion"
        "(left_kp_id, right_kp_id, left_front_norm, right_front_norm, "
        " action_id, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(left_kp_id, right_kp_id) DO UPDATE SET "
        "left_front_norm = excluded.left_front_norm, "
        "right_front_norm = excluded.right_front_norm, "
        "action_id = excluded.action_id, created_at = excluded.created_at",
        (
            left_id,
            right_id,
            first["front_norm"] or "",
            second["front_norm"] or "",
            action_id,
            now,
        ),
    )


def _active_exclusion_pairs(conn) -> set[tuple[int, int]]:
    rows = conn.execute(
        "SELECT e.left_kp_id, e.right_kp_id, "
        "e.left_front_norm, e.right_front_norm, "
        "lk.front_norm AS left_norm, rk.front_norm AS right_norm "
        "FROM dup_exclusion e "
        "JOIN knowledge_point lk ON lk.id = e.left_kp_id "
        "JOIN knowledge_point rk ON rk.id = e.right_kp_id"
    ).fetchall()
    active: set[tuple[int, int]] = set()
    stale: list[tuple[int, int]] = []
    for row in rows:
        unchanged = (
            (row["left_front_norm"] or "") == (row["left_norm"] or "")
            and (row["right_front_norm"] or "") == (row["right_norm"] or "")
        )
        if unchanged:
            active.add((row["left_kp_id"], row["right_kp_id"]))
        else:
            stale.append((row["left_kp_id"], row["right_kp_id"]))
    if stale:
        conn.executemany(
            "DELETE FROM dup_exclusion WHERE left_kp_id = ? AND right_kp_id = ?",
            stale,
        )
        conn.commit()
    return active


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


def _resolve(conn, candidate, action: str, keep_side: str = "left") -> dict:
    if keep_side not in ("left", "right"):
        raise ReviewError("不支持的保留方向")
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

    survivor = left if keep_side == "left" else right
    discarded = right if keep_side == "left" else left
    survivor_id = survivor["id"]
    discarded_id = discarded["id"]

    if action == "merge":
        kps = [dict(left), dict(right)]
        mentions = _snapshot(conn, "mention", "WHERE kp_id = ?", (discarded_id,))
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
        merged_back = survivor["back_md"] or ""
        if discarded["back_md"] and discarded["back_md"].strip() != merged_back.strip():
            merged_back = (
                f"{merged_back.rstrip()}\n\n---\n\n{discarded['back_md'].strip()}"
                if merged_back.strip()
                else discarded["back_md"].strip()
            )

        conn.execute(
            "UPDATE mention SET kp_id = ?, status = 'attached' WHERE kp_id = ?",
            (survivor_id, discarded_id),
        )

        note_id = survivor["anki_note_id"]
        if note_id is None and discarded["anki_note_id"] is not None:
            note_id = discarded["anki_note_id"]
            survivor_sync = conn.execute(
                "SELECT 1 FROM anki_sync WHERE kp_id = ?", (survivor_id,)
            ).fetchone()
            if survivor_sync is None:
                discarded_sync = conn.execute(
                    "SELECT * FROM anki_sync WHERE kp_id = ?", (discarded_id,)
                ).fetchone()
                if discarded_sync is not None:
                    conn.execute(
                        "DELETE FROM anki_sync WHERE kp_id = ?", (discarded_id,)
                    )
                    conn.execute(
                        "INSERT INTO anki_sync"
                        "(kp_id, anki_note_id, pushed_hash, pushed_deck, pushed_at, last_error) "
                        "VALUES(?, ?, NULL, ?, ?, ?)",
                        (
                            survivor_id,
                            discarded_sync["anki_note_id"],
                            discarded_sync["pushed_deck"],
                            discarded_sync["pushed_at"],
                            discarded_sync["last_error"],
                        ),
                    )

        status = survivor["status"]
        if merged_back.strip() and status == "draft":
            status = "ready"

        conn.execute(
            "UPDATE knowledge_point SET back_md = ?, status = ?, anki_note_id = ?, "
            "updated_at = ? WHERE id = ?",
            (merged_back, status, note_id, now, survivor_id),
        )
        conn.execute(
            "UPDATE anki_sync SET pushed_hash = NULL, pushed_front_hash = NULL, "
            "pushed_back_hash = NULL WHERE kp_id = ?",
            (survivor_id,),
        )
        aliases.record_merge(conn, action_id, survivor_id, [discarded])
        conn.execute("DELETE FROM knowledge_point WHERE id = ?", (discarded_id,))

    elif action == "keep":
        if mention_id is not None:
            conn.execute(
                "UPDATE mention SET status = 'attached' WHERE id = ?", (mention_id,)
            )
        conn.execute(
            "UPDATE dup_candidate SET status = 'kept', resolved_at = ? WHERE id = ?",
            (now, candidate["id"]),
        )
        _record_exclusion(conn, left, right, action_id, now)

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


def resolve_candidate(
    candidate_id: int, action: str, keep_side: str = "left"
) -> dict:
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
        result = _resolve(conn, candidate, action, keep_side)
    return {"candidate_id": candidate_id, "action": action, **result}


def resolve_batch(
    candidate_ids: list[int], action: str, keep_side: str = "left"
) -> dict:
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
            resolve_candidate(candidate_id, action, keep_side)
            resolved.append(candidate_id)
        except ReviewError as exc:
            errors.append({"id": candidate_id, "error": str(exc)})
    return {"resolved": resolved, "errors": errors}


def merge_cards(survivor_id: int, source_ids: list[int]) -> dict:
    """把手动多选的卡片并入一张存活卡，整次合并只留一条可撤销记录。"""
    sources: list[int] = []
    for raw in source_ids:
        cid = int(raw)
        if cid == survivor_id:
            raise ReviewError("存活卡片不能同时作为被合并卡片")
        if cid in sources:
            raise ReviewError("被合并卡片重复")
        sources.append(cid)
    if not sources:
        raise ReviewError("至少选择两张卡片才能合并")

    all_ids = [survivor_id, *sources]
    placeholders = ", ".join("?" for _ in all_ids)
    source_placeholders = ", ".join("?" for _ in sources)

    with closing(connect()) as conn, conn:
        rows = {}
        for cid in all_ids:
            row = conn.execute(
                "SELECT * FROM knowledge_point WHERE id = ?", (cid,)
            ).fetchone()
            if row is None:
                raise ReviewError(f"卡片 {cid} 不存在", 404)
            rows[cid] = row
        survivor = rows[survivor_id]
        source_rows = [rows[cid] for cid in sources]

        kps = [dict(rows[cid]) for cid in all_ids]
        mentions = _snapshot(
            conn, "mention", f"WHERE kp_id IN ({source_placeholders})", tuple(sources)
        )
        syncs = _snapshot(
            conn, "anki_sync", f"WHERE kp_id IN ({placeholders})", tuple(all_ids)
        )
        candidates = _snapshot(
            conn,
            "dup_candidate",
            f"WHERE left_kp_id IN ({source_placeholders}) "
            f"OR right_kp_id IN ({source_placeholders})",
            (*sources, *sources),
        )

        note_id = survivor["anki_note_id"]
        note_from = survivor_id if note_id is not None else None
        if note_id is None:
            note_cards = [row for row in source_rows if row["anki_note_id"] is not None]

            def priority(row) -> tuple:
                sync = conn.execute(
                    "SELECT pushed_at FROM anki_sync WHERE kp_id = ?", (row["id"],)
                ).fetchone()
                return (
                    1 if row["status"] == "ready" else 0,
                    (sync["pushed_at"] or "") if sync is not None else "",
                    row["updated_at"] or "",
                )

            if note_cards:
                picked = max(note_cards, key=priority)
                note_id = picked["anki_note_id"]
                note_from = picked["id"]
                picked_sync = conn.execute(
                    "SELECT * FROM anki_sync WHERE kp_id = ?", (picked["id"],)
                ).fetchone()
                conn.execute("DELETE FROM anki_sync WHERE kp_id = ?", (survivor_id,))
                if picked_sync is not None:
                    conn.execute(
                        "INSERT INTO anki_sync"
                        "(kp_id, anki_note_id, pushed_hash, pushed_deck, pushed_at, last_error) "
                        "VALUES(?, ?, NULL, ?, ?, ?)",
                        (
                            survivor_id,
                            picked_sync["anki_note_id"],
                            picked_sync["pushed_deck"],
                            picked_sync["pushed_at"],
                            picked_sync["last_error"],
                        ),
                    )

        unclaimed = [
            {
                "kp_id": row["id"],
                "note_id": row["anki_note_id"],
                "front_md": row["front_md"],
            }
            for row in source_rows
            if row["anki_note_id"] is not None and row["id"] != note_from
        ]

        merged_back = (survivor["back_md"] or "").strip()
        sections = {merged_back} if merged_back else set()
        for row in sorted(source_rows, key=lambda item: item["id"]):
            part = (row["back_md"] or "").strip()
            if not part or part in sections:
                continue
            sections.add(part)
            merged_back = f"{merged_back}\n\n---\n\n{part}" if merged_back else part

        now = utc_now()
        payload = {
            "action": "merge_cards",
            "candidate": None,
            "survivor_id": survivor_id,
            "kps": kps,
            "mentions": mentions,
            "syncs": syncs,
            "candidates": candidates,
            "summary": {
                "left_front": survivor["front_md"],
                "right_front": " / ".join(row["front_md"] for row in source_rows),
                "mention_text": None,
            },
        }
        cursor = conn.execute(
            "INSERT INTO review_action(candidate_id, action, payload, created_at) "
            "VALUES(NULL, ?, ?, ?)",
            ("merge_cards", json.dumps(payload, ensure_ascii=False), now),
        )
        action_id = cursor.lastrowid

        conn.execute(
            f"UPDATE mention SET kp_id = ?, status = 'attached' "
            f"WHERE kp_id IN ({source_placeholders})",
            (survivor_id, *sources),
        )
        conn.execute(
            "UPDATE knowledge_point SET back_md = ?, anki_note_id = ?, "
            "updated_at = ? WHERE id = ?",
            (merged_back, note_id, now, survivor_id),
        )
        aliases.record_merge(conn, action_id, survivor_id, source_rows)
        conn.execute(
            f"DELETE FROM knowledge_point WHERE id IN ({source_placeholders})",
            tuple(sources),
        )
        conn.execute(
            "UPDATE anki_sync SET pushed_hash = NULL, pushed_front_hash = NULL, "
            "pushed_back_hash = NULL WHERE kp_id = ?",
            (survivor_id,),
        )

    return {
        "survivor_id": survivor_id,
        "merged_ids": sources,
        "action_id": action_id,
        "anki_note_id": note_id,
        "note_from": note_from,
        "unclaimed_notes": unclaimed,
    }


def list_actions(page: int = 1, page_size: int = 10) -> dict:
    page = max(1, page)
    page_size = min(50, max(1, page_size))
    with closing(connect()) as conn:
        total = conn.execute("SELECT COUNT(*) AS value FROM review_action").fetchone()[
            "value"
        ]
        rows = conn.execute(
            "SELECT * FROM review_action ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
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
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


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

        if row["action"] in ("merge", "merge_cards"):
            aliases.remove_merge(conn, action_id)
            kp_ids = [snapshot["id"] for snapshot in payload.get("kps") or []]
            if kp_ids:
                conn.execute(
                    "DELETE FROM anki_sync WHERE kp_id IN "
                    f"({', '.join('?' for _ in kp_ids)})",
                    kp_ids,
                )
        elif row["action"] == "keep":
            conn.execute(
                "DELETE FROM dup_exclusion WHERE action_id = ?", (action_id,)
            )

        for snapshot in payload.get("kps") or []:
            _restore(conn, "knowledge_point", snapshot, "id")
        for snapshot in payload.get("mentions") or []:
            _restore(conn, "mention", snapshot, "id")
        for snapshot in payload.get("syncs") or []:
            _restore(conn, "anki_sync", snapshot, "kp_id")
        for snapshot in payload.get("candidates") or []:
            _restore(conn, "dup_candidate", snapshot, "id")

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
    alias_reviewed = 0
    alias_inserted = 0
    alias_above = 0

    with closing(connect()) as conn:
        merger = Merger(conn, client, settings)
        if not merger.vector_ready:
            raise ReviewError(merger.vector_error or "向量检索不可用")

        _notify(progress, "补全向量", 0, 1)
        embeddings = merger.backfill()
        _notify(progress, "补全向量", 1, 1)
        if not merger.vector_ready:
            raise ReviewError(merger.vector_error or "向量检索不可用")
        if merger.alias_error:
            errors.append(f"别名向量补全失败: {merger.alias_error}")

        vectors = merger.store.load_all()
        rows = conn.execute(
            "SELECT id, front_md, module_id FROM knowledge_point ORDER BY id"
        ).fetchall()
        items = [
            (row["id"], vectors[row["id"]])
            for row in rows
            if row["id"] in vectors
        ]
        fronts = {row["id"]: row["front_md"] for row in rows}
        alias_rows = aliases.load_all(conn, merger.dim)
        excluded = _active_exclusion_pairs(conn)

        scored: list[tuple[float, int, int]] = []
        seen: set[tuple[int, int]] = set()
        total_rows = len(items)
        scan_k = 50
        for index, (left_id, left_vector) in enumerate(items):
            try:
                hits = merger.store.search(left_vector, k=scan_k)
            except sqlite3.Error as exc:
                errors.append(f"相似度检索失败: {exc}")
                break
            for right_id, score in hits:
                if right_id == left_id or right_id not in fronts:
                    continue
                key = _pair_key(left_id, right_id)
                if key in seen:
                    continue
                seen.add(key)
                if key in excluded or score < low:
                    continue
                scored.append((score, key[0], key[1]))
            if index % 20 == 0:
                _notify(progress, "相似度检索", index, total_rows)

        pairs = len(seen)
        above = len(scored)
        scored.sort(key=lambda item: item[0], reverse=True)
        _notify(progress, "相似度检索", total_rows, total_rows)

        alias_best: dict[tuple[int, int], tuple[float, int, int, str]] = {}
        for index, alias_row in enumerate(alias_rows, start=1):
            owner = aliases.resolve_owner(conn, alias_row["owner_kp_id"])
            if owner is None or owner not in fronts:
                continue
            try:
                alias_hits = merger.store.search(alias_row["vector"], k=30)
            except sqlite3.Error as exc:
                errors.append(f"别名检索失败: {exc}")
                break
            for active_id, score in alias_hits:
                if active_id == owner or active_id not in fronts or score < low:
                    continue
                if _pair_key(owner, active_id) in excluded:
                    continue
                key = (owner, active_id)
                current = alias_best.get(key)
                if current is None or score > current[0]:
                    alias_best[key] = (
                        score,
                        owner,
                        active_id,
                        alias_row["front_md"],
                    )
            if index % 20 == 0:
                _notify(progress, "别名比对", index, len(alias_rows))
        alias_scored = sorted(
            alias_best.values(), key=lambda item: item[0], reverse=True
        )
        alias_above = len(alias_scored)
        _notify(progress, "别名比对", len(alias_rows), len(alias_rows))

        existing = {
            frozenset((row["left_kp_id"], row["right_kp_id"]))
            for row in conn.execute(
                "SELECT left_kp_id, right_kp_id FROM dup_candidate "
                "WHERE status = 'pending'"
            )
        }
        created_pairs = set(existing)
        shortlist = [
            item
            for item in scored
            if frozenset((item[1], item[2])) not in existing
            and _pair_key(item[1], item[2]) not in excluded
        ][:SCAN_VERIFY_LIMIT]
        alias_shortlist = [
            item
            for item in alias_scored
            if frozenset((item[1], item[2])) not in existing
            and _pair_key(item[1], item[2]) not in excluded
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
            created_pairs.add(frozenset((left_id, right_id)))
        _notify(progress, "LLM 复核", len(shortlist), len(shortlist))

        for index, (score, owner_id, active_id, alias_front) in enumerate(
            alias_shortlist, start=1
        ):
            pair = frozenset((owner_id, active_id))
            if pair in created_pairs:
                continue
            _notify(progress, "别名复核", index - 1, len(alias_shortlist))
            try:
                same, reason = _verify_same(
                    client, fronts[active_id], alias_front
                )
            except AIError as exc:
                errors.append(f"alias {owner_id}-{active_id}: {exc}")
                continue
            alias_reviewed += 1
            if not same:
                continue
            if pair in created_pairs:
                continue
            reason_text = (
                f"{reason or f'相似度 {score:.2f}'}（与已合并来源"
                f"「{alias_front[:60]}」相似）"
            )
            conn.execute(
                "INSERT INTO dup_candidate"
                "(left_kp_id, right_kp_id, mention_id, score, reason, created_at) "
                "VALUES(?, ?, NULL, ?, ?, ?)",
                (owner_id, active_id, score, reason_text, utc_now()),
            )
            conn.commit()
            created_pairs.add(pair)
            alias_inserted += 1
        _notify(progress, "别名复核", len(alias_shortlist), len(alias_shortlist))

    return {
        "knowledge_points": total_rows,
        "pairs": pairs,
        "above_threshold": above,
        "reviewed": reviewed,
        "candidates": inserted,
        "aliases": len(alias_rows),
        "alias_above_threshold": alias_above,
        "alias_reviewed": alias_reviewed,
        "alias_candidates": alias_inserted,
        "embeddings": embeddings,
        "errors": errors,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
