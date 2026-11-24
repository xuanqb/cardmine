"""SQLite 连接与版本化迁移。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _subtree_card_count(conn: sqlite3.Connection, root_id: int) -> int:
    row = conn.execute(
        """
        WITH RECURSIVE sub(id) AS (
            SELECT id FROM module WHERE id = ?
            UNION ALL
            SELECT m.id FROM module m JOIN sub ON m.parent_id = sub.id
        )
        SELECT COUNT(*) AS value FROM knowledge_point
        WHERE module_id IN (SELECT id FROM sub)
        """,
        (root_id,),
    ).fetchone()
    return int(row["value"])


def _normalize_ai_roots(conn: sqlite3.Connection) -> None:
    """把 AI 创建的顶层模块挂到卡片量最大的非 AI 顶层模块下，避免出现平行分组。"""
    roots = conn.execute(
        "SELECT id, name, path, created_by FROM module WHERE parent_id IS NULL"
    ).fetchall()
    humans = [row for row in roots if (row["created_by"] or "") != "ai"]
    ai_roots = [row for row in roots if row["created_by"] == "ai"]
    if not humans or not ai_roots:
        return

    target = max(
        humans,
        key=lambda row: (
            _subtree_card_count(conn, row["id"]),
            -row["id"],
        ),
    )
    if target["name"] == "Default" and _subtree_card_count(conn, target["id"]) == 0:
        return

    for root in ai_roots:
        old = root["path"]
        new = f"{target['path']}/{old}"
        escaped = new.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        exists = conn.execute(
            "SELECT 1 FROM module WHERE path = ? OR path LIKE ? ESCAPE '\\'",
            (new, escaped + "/%"),
        ).fetchone()
        if exists is not None:
            continue

        old_like = old.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn.execute(
            "UPDATE module SET path = ? || substr(path, ?) "
            "WHERE path = ? OR path LIKE ? ESCAPE '\\'",
            (new, len(old) + 1, old, old_like + "/%"),
        )
        sort = conn.execute(
            "SELECT COALESCE(MAX(sort), 0) + 1 AS value FROM module WHERE parent_id = ?",
            (target["id"],),
        ).fetchone()["value"]
        conn.execute(
            "UPDATE module SET parent_id = ?, sort = ? WHERE id = ?",
            (target["id"], sort, root["id"]),
        )
        # 被并入的顶层模块不再是组，归属它的卡片、别名与来源改指向新组
        conn.execute(
            "UPDATE knowledge_point SET group_id = ? WHERE group_id = ?",
            (target["id"], root["id"]),
        )
        conn.execute(
            "UPDATE kp_alias SET group_id = ? WHERE group_id = ?",
            (target["id"], root["id"]),
        )
        conn.execute(
            "UPDATE feed SET group_id = ? WHERE group_id = ?",
            (target["id"], root["id"]),
        )


def init_db() -> None:
    """按文件名前缀顺序应用未执行的迁移，版本记录在 PRAGMA user_version。"""
    config.ensure_dirs()

    with closing(connect()) as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version <= current:
                continue
            script = path.read_text(encoding="utf-8")
            conn.executescript(
                f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        _normalize_ai_roots(conn)
        conn.commit()


def schema_version() -> int:
    with closing(connect()) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
