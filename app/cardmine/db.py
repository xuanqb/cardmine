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


def schema_version() -> int:
    with closing(connect()) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
