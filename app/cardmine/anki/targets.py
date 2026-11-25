"""同步目标与组映射：一个目标是一个独立 Anki 集合，一个组可映射多个目标。"""

from __future__ import annotations

from ..db import utc_now
from .client import AnkiClient

TARGET_FIELDS = {"name", "base_url", "enabled", "sort"}


def _to_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def list_targets(conn, enabled_only: bool = False) -> list[dict]:
    clause = "WHERE enabled = 1" if enabled_only else ""
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM anki_target {clause} ORDER BY sort, id"
        )
    ]


def get_target(conn, target_id: int):
    return conn.execute("SELECT * FROM anki_target WHERE id = ?", (target_id,)).fetchone()


def create_target(
    conn,
    name: str,
    base_url: str,
    enabled: bool = True,
    sort: int | None = None,
) -> dict:
    if sort is None:
        sort = conn.execute(
            "SELECT COALESCE(MAX(sort), -1) + 1 AS value FROM anki_target"
        ).fetchone()["value"]
    cursor = conn.execute(
        "INSERT INTO anki_target(name, base_url, enabled, sort, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (name.strip(), base_url.strip().rstrip("/"), 1 if enabled else 0, sort, utc_now()),
    )
    return dict(get_target(conn, cursor.lastrowid))


def update_target(conn, target_id: int, **fields) -> dict | None:
    updates = {key: value for key, value in fields.items() if key in TARGET_FIELDS}
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    if "base_url" in updates and updates["base_url"]:
        updates["base_url"] = updates["base_url"].strip().rstrip("/")
    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE anki_target SET {assignments} WHERE id = ?",
            [*updates.values(), target_id],
        )
    return _to_dict(get_target(conn, target_id))


def delete_target(conn, target_id: int) -> bool:
    cursor = conn.execute("DELETE FROM anki_target WHERE id = ?", (target_id,))
    return cursor.rowcount > 0


def all_groups(conn) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, name, path FROM module WHERE parent_id IS NULL ORDER BY path"
        )
    ]


def target_group_ids(conn, target_id: int) -> list[int]:
    return [
        int(row["group_id"])
        for row in conn.execute(
            "SELECT group_id FROM group_target WHERE target_id = ? ORDER BY group_id",
            (target_id,),
        )
    ]


def set_target_groups(conn, target_id: int, group_ids: list[int]) -> list[int]:
    """覆盖目标的组映射；只接受顶层模块。"""
    wanted: list[int] = []
    for group_id in dict.fromkeys(int(item) for item in group_ids):
        row = conn.execute(
            "SELECT 1 FROM module WHERE id = ? AND parent_id IS NULL", (group_id,)
        ).fetchone()
        if row is not None:
            wanted.append(group_id)
    conn.execute("DELETE FROM group_target WHERE target_id = ?", (target_id,))
    conn.executemany(
        "INSERT INTO group_target(group_id, target_id) VALUES(?, ?)",
        [(group_id, target_id) for group_id in wanted],
    )
    return wanted


def group_target_ids(conn, group_id: int) -> list[int]:
    return [
        int(row["target_id"])
        for row in conn.execute(
            "SELECT target_id FROM group_target WHERE group_id = ? ORDER BY target_id",
            (group_id,),
        )
    ]


def card_target_ids(conn, kp_id: int) -> list[int]:
    return [
        int(row["target_id"])
        for row in conn.execute(
            """
            SELECT gt.target_id
            FROM knowledge_point kp
            JOIN group_target gt ON gt.group_id = kp.group_id
            JOIN anki_target t ON t.id = gt.target_id AND t.enabled = 1
            WHERE kp.id = ?
            ORDER BY t.sort, t.id
            """,
            (kp_id,),
        )
    ]


def scope_paths(conn, target_id: int) -> list[str]:
    return [
        row["path"]
        for row in conn.execute(
            """
            SELECT m.path
            FROM group_target gt JOIN module m ON m.id = gt.group_id
            WHERE gt.target_id = ?
            ORDER BY m.path
            """,
            (target_id,),
        )
    ]


def deck_in_scope(deck: str | None, scopes: list[str]) -> bool:
    path = (deck or "").replace("::", "/").strip("/")
    if not path:
        return False
    return any(path == root or path.startswith(root + "/") for root in scopes)


def group_for_deck(conn, target_id: int, deck: str | None):
    """返回牌组所属的、已映射到该目标的顶层组；不在作用域时返回 None。"""
    path = (deck or "").replace("::", "/").strip("/")
    if not path:
        return None
    row = conn.execute(
        """
        SELECT m.id, m.name, m.path
        FROM group_target gt JOIN module m ON m.id = gt.group_id
        WHERE gt.target_id = ?
          AND (m.path = ? OR ? LIKE m.path || '/%')
        ORDER BY length(m.path) DESC
        LIMIT 1
        """,
        (target_id, path, path),
    ).fetchone()
    return row


def client_for(target, guard: bool = True) -> AnkiClient:
    return AnkiClient(target["base_url"], conflict_guard=guard)


def record_target_sync(
    conn, target_id: int, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE anki_target SET last_sync_at = ?, last_error = ? WHERE id = ?",
        (utc_now(), error, target_id),
    )


def target_stats(conn, target_id: int) -> dict:
    groups = target_group_ids(conn, target_id)
    placeholders = ",".join("?" for _ in groups) or "NULL"
    ready = conn.execute(
        f"SELECT COUNT(*) AS value FROM knowledge_point "
        f"WHERE status = 'ready' AND group_id IN ({placeholders})",
        groups,
    ).fetchone()["value"]
    linked = conn.execute(
        "SELECT COUNT(*) AS value FROM anki_sync "
        "WHERE target_id = ? AND anki_note_id IS NOT NULL AND detached = 0",
        (target_id,),
    ).fetchone()["value"]
    detached = conn.execute(
        "SELECT COUNT(*) AS value FROM anki_sync "
        "WHERE target_id = ? AND detached = 1",
        (target_id,),
    ).fetchone()["value"]
    conflicts = conn.execute(
        "SELECT COUNT(*) AS value FROM sync_conflict "
        "WHERE target_id = ? AND status = 'pending'",
        (target_id,),
    ).fetchone()["value"]
    synced = conn.execute(
        "SELECT COUNT(*) AS value FROM anki_sync s "
        "JOIN knowledge_point kp ON kp.id = s.kp_id "
        f"WHERE s.target_id = ? AND kp.status = 'ready' "
        f"AND s.anki_note_id IS NOT NULL AND s.pushed_hash IS NOT NULL "
        f"AND kp.group_id IN ({placeholders})",
        (target_id, *groups),
    ).fetchone()["value"]
    target = get_target(conn, target_id)
    return {
        "groups": groups,
        "group_count": len(groups),
        "ready": ready,
        "linked": linked,
        "pending": max(0, ready - synced),
        "detached": detached,
        "conflicts": conflicts,
        "last_sync_at": target["last_sync_at"] if target else None,
        "last_error": target["last_error"] if target else None,
    }
