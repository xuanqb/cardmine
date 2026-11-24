import sqlite3
from contextlib import closing

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import aliases, config, review
from ..anki.client import AnkiClient, AnkiError
from ..db import connect, utc_now
from ..importer.textfmt import normalize_front
from .settings import load_settings

router = APIRouter(prefix="/api", tags=["cards"])

SORTS = {
    "updated_desc": "kp.updated_at DESC, kp.id DESC",
    "mentions_desc": "mention_count DESC, kp.updated_at DESC",
    "recent_desc": "(last_mention_at IS NULL), last_mention_at DESC, kp.updated_at DESC",
}
STATUSES = {"draft", "ready", "archived"}


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _module_ids(conn, module_id: int) -> list[int]:
    """返回该模块及其所有子模块的 id。"""
    row = conn.execute("SELECT path FROM module WHERE id = ?", (module_id,)).fetchone()
    if row is None:
        return []
    path = row["path"]
    rows = conn.execute(
        "SELECT id FROM module WHERE path = ? OR path LIKE ? ESCAPE '\\'",
        (path, _like_escape(path) + "/%"),
    ).fetchall()
    return [item["id"] for item in rows]


@router.get("/cards")
def list_cards(
    module_id: int | None = None,
    status: list[str] | None = Query(None),
    flagged: bool = False,
    q: str | None = None,
    sort: str = "updated_desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    if sort not in SORTS:
        raise HTTPException(status_code=400, detail="不支持的排序方式")
    if status is not None and any(item not in STATUSES for item in status):
        raise HTTPException(status_code=400, detail="不支持的状态")

    where: list[str] = []
    params: list = []

    with closing(connect()) as conn:
        if module_id is not None:
            ids = _module_ids(conn, module_id)
            if not ids:
                return {"items": [], "total": 0, "page": page, "page_size": page_size}
            placeholders = ",".join("?" for _ in ids)
            where.append(f"kp.module_id IN ({placeholders})")
            params.extend(ids)

        if status:
            placeholders = ",".join("?" for _ in status)
            where.append(f"kp.status IN ({placeholders})")
            params.extend(status)

        if q and q.strip():
            pattern = f"%{_like_escape(q.strip())}%"
            where.append(
                "(kp.front_md LIKE ? ESCAPE '\\' OR kp.back_md LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern])

        if flagged:
            where.append(
                "(COALESCE(s.anki_flag, 0) > 0 OR COALESCE(s.flag_clear_pending, 0) > 0)"
            )

        clause = f"WHERE {' AND '.join(where)}" if where else ""

        total = conn.execute(
            f"""
            SELECT COUNT(*) FROM knowledge_point kp
            LEFT JOIN anki_sync s ON s.kp_id = kp.id
            {clause}
            """,
            params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT kp.id, kp.front_md, kp.status, kp.updated_at,
                   kp.origin, kp.ai_status,
                   (TRIM(COALESCE(kp.ai_back_md, '')) != '') AS has_ai_answer,
                   COALESCE(s.anki_flag, 0) AS anki_flag,
                   COALESCE(s.flag_clear_pending, 0) AS flag_clear_pending,
                   m.path AS module_path,
                   (SELECT COUNT(*) FROM mention me WHERE me.kp_id = kp.id) AS mention_count,
                   (SELECT MAX(me.created_at) FROM mention me WHERE me.kp_id = kp.id)
                       AS last_mention_at
            FROM knowledge_point kp
            LEFT JOIN module m ON m.id = kp.module_id
            LEFT JOIN anki_sync s ON s.kp_id = kp.id
            {clause}
            ORDER BY {SORTS[sort]}
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()

    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _fetch_detail(conn: sqlite3.Connection, card_id: int) -> dict:
    row = conn.execute(
        """
        SELECT kp.*, m.path AS module_path,
               COALESCE(s.anki_flag, 0) AS anki_flag,
               COALESCE(s.flag_clear_pending, 0) AS flag_clear_pending
        FROM knowledge_point kp
        LEFT JOIN module m ON m.id = kp.module_id
        LEFT JOIN anki_sync s ON s.kp_id = kp.id
        WHERE kp.id = ?
        """,
        (card_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="卡片不存在")

    variants = conn.execute(
        """
        SELECT me.id, me.raw_text, me.section, me.status AS mention_status,
               me.created_at, p.id AS post_id, p.title AS post_title,
               p.url AS post_url, p.fetched_at
        FROM mention me
        JOIN post p ON p.id = me.post_id
        WHERE me.kp_id = ?
        ORDER BY me.created_at DESC, me.id DESC
        """,
        (card_id,),
    ).fetchall()

    sources = conn.execute(
        """
        SELECT DISTINCT p.id, p.title, p.url, p.fetched_at
        FROM mention me
        JOIN post p ON p.id = me.post_id
        WHERE me.kp_id = ?
        ORDER BY p.fetched_at DESC
        """,
        (card_id,),
    ).fetchall()

    detail = dict(row)
    detail["ai_stale"] = bool(
        (detail.get("ai_back_md") or "").strip()
        and detail.get("ai_front_norm")
        and detail["ai_front_norm"] != detail.get("front_norm")
    )
    detail["mention_count"] = len(variants)
    detail["last_mention_at"] = variants[0]["created_at"] if variants else None
    detail["variants"] = [dict(item) for item in variants]
    detail["sources"] = [dict(item) for item in sources]
    detail["aliases"] = aliases.list_for_survivor(conn, card_id)
    detail["conflicts"] = [
        dict(item)
        for item in conn.execute(
            """
            SELECT id, field, note, system_value, anki_value, created_at
            FROM anki_conflict
            WHERE kp_id = ? AND status = 'pending'
            ORDER BY id
            """,
            (card_id,),
        )
    ]
    return detail


@router.get("/cards/{card_id}")
def get_card(card_id: int) -> dict:
    with closing(connect()) as conn:
        return _fetch_detail(conn, card_id)


@router.post("/cards/{card_id}/flag/clear")
def clear_anki_flag(card_id: int) -> dict:
    """登记清除 Anki 旗帜；真正的清除在下一次推送时执行。"""
    with closing(connect()) as conn, conn:
        row = conn.execute(
            """
            SELECT COALESCE(s.anki_flag, 0) AS anki_flag
            FROM knowledge_point kp
            LEFT JOIN anki_sync s ON s.kp_id = kp.id
            WHERE kp.id = ?
            """,
            (card_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        if not row["anki_flag"]:
            raise HTTPException(
                status_code=400, detail="这张卡没有从 Anki 同步到旗帜"
            )
        conn.execute(
            "INSERT INTO anki_sync(kp_id, flag_clear_pending) VALUES(?, ?) "
            "ON CONFLICT(kp_id) DO UPDATE SET "
            "flag_clear_pending = excluded.flag_clear_pending",
            (card_id, row["anki_flag"]),
        )
        return _fetch_detail(conn, card_id)


@router.delete("/cards/{card_id}/aliases/{alias_id}")
def delete_card_alias(card_id: int, alias_id: int) -> dict:
    with closing(connect()) as conn, conn:
        if conn.execute(
            "SELECT 1 FROM knowledge_point WHERE id = ?", (card_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        if not aliases.delete_for_card(conn, card_id, alias_id):
            raise HTTPException(status_code=404, detail="来源问题不存在")
        return _fetch_detail(conn, card_id)


class CardMerge(BaseModel):
    survivor_id: int = Field(ge=1)
    source_ids: list[int] = Field(min_length=1)
    delete_unclaimed_notes: bool = False


def _delete_notes(note_ids: list[int]) -> tuple[list[int], list[str]]:
    if not note_ids:
        return [], []
    settings = load_settings()
    client = AnkiClient(settings["anki_url"] or config.ANKI_CONNECT_URL)
    try:
        client.call("deleteNotes", timeout=15, notes=note_ids)
    except AnkiError as exc:
        joined = ", ".join(str(item) for item in note_ids)
        return [], [f"note {joined}: {exc}"]
    return note_ids, []


@router.post("/cards/merge")
def merge_cards(payload: CardMerge) -> dict:
    try:
        result = review.merge_cards(payload.survivor_id, payload.source_ids)
    except review.ReviewError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    result["deleted_notes"] = []
    result["note_delete_errors"] = []
    if payload.delete_unclaimed_notes and result["unclaimed_notes"]:
        note_ids = [
            item["note_id"]
            for item in result["unclaimed_notes"]
            if item["note_id"] != result["anki_note_id"]
        ]
        deleted, errors = _delete_notes(note_ids)
        result["deleted_notes"] = deleted
        result["note_delete_errors"] = errors

    with closing(connect()) as conn:
        result["card"] = _fetch_detail(conn, payload.survivor_id)
    return result


class CardArchive(BaseModel):
    ids: list[int] = Field(min_length=1)
    dry_run: bool = False


@router.post("/cards/archive")
def archive_cards(payload: CardArchive) -> dict:
    ids = list(dict.fromkeys(payload.ids))
    placeholders = ",".join("?" for _ in ids)

    with closing(connect()) as conn, conn:
        rows = conn.execute(
            f"""
            SELECT kp.id, kp.status,
                   COALESCE(kp.anki_note_id, s.anki_note_id) AS note_id
            FROM knowledge_point kp
            LEFT JOIN anki_sync s ON s.kp_id = kp.id
            WHERE kp.id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        found = {row["id"]: row for row in rows}

        missing = [card_id for card_id in ids if card_id not in found]
        skipped = [
            card_id
            for card_id in ids
            if card_id in found and found[card_id]["status"] == "archived"
        ]
        to_archive = [
            card_id
            for card_id in ids
            if card_id in found and found[card_id]["status"] != "archived"
        ]

        if payload.dry_run:
            return {
                "dry_run": True,
                "to_archive": len(to_archive),
                "skipped": len(skipped),
                "failed": len(missing),
                "linked_notes": sum(
                    1 for card_id in to_archive if found[card_id]["note_id"]
                ),
            }

        if to_archive:
            placeholders = ",".join("?" for _ in to_archive)
            conn.execute(
                f"UPDATE knowledge_point SET status = 'archived', updated_at = ? "
                f"WHERE id IN ({placeholders})",
                [utc_now(), *to_archive],
            )

    return {
        "archived_ids": to_archive,
        "skipped_ids": skipped,
        "failed": [{"id": card_id, "reason": "卡片不存在"} for card_id in missing],
    }


class CardUpdate(BaseModel):
    front_md: str | None = None
    back_md: str | None = None
    module_id: int | None = Field(None, ge=1)
    status: str | None = None


@router.patch("/cards/{card_id}")
def update_card(card_id: int, payload: CardUpdate) -> dict:
    updates = payload.model_dump(exclude_unset=True)

    with closing(connect()) as conn, conn:
        current = conn.execute(
            "SELECT id, status, back_md FROM knowledge_point WHERE id = ?", (card_id,)
        ).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="卡片不存在")

        fields: list[str] = []
        params: list = []

        if "front_md" in updates:
            front_md = (updates["front_md"] or "").strip()
            if not normalize_front(front_md):
                raise HTTPException(status_code=400, detail="正面不能为空")
            front_norm = normalize_front(front_md)
            collision = conn.execute(
                "SELECT id FROM knowledge_point WHERE front_norm = ? AND id != ?",
                (front_norm, card_id),
            ).fetchone()
            if collision:
                raise HTTPException(
                    status_code=409, detail="该问法已存在，请使用合并功能"
                )
            fields.extend(["front_md = ?", "front_norm = ?"])
            params.extend([front_md, front_norm])

        if "back_md" in updates:
            fields.append("back_md = ?")
            params.append(updates["back_md"] or "")

        if "module_id" in updates:
            exists = conn.execute(
                "SELECT 1 FROM module WHERE id = ?", (updates["module_id"],)
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=400, detail="模块不存在")
            fields.append("module_id = ?")
            params.append(updates["module_id"])

        if "status" in updates:
            status = updates["status"]
            if status not in STATUSES:
                raise HTTPException(status_code=400, detail="不支持的状态")
            fields.append("status = ?")
            params.append(status)
        else:
            new_back = updates.get("back_md")
            if (
                new_back is not None
                and new_back.strip()
                and current["status"] == "draft"
            ):
                fields.append("status = 'ready'")

        if fields:
            fields.append("updated_at = ?")
            params.append(utc_now())
            params.append(card_id)
            conn.execute(
                f"UPDATE knowledge_point SET {', '.join(fields)} WHERE id = ?", params
            )

        return _fetch_detail(conn, card_id)
