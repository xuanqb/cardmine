from fastapi import APIRouter

from .. import config, db
from ..anki import targets

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict:
    conn = db.connect()
    try:
        conn.execute("SELECT 1").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        target_rows = targets.list_targets(conn)
    finally:
        conn.close()

    return {
        "status": "ok",
        "app_version": config.APP_VERSION,
        "db": "ok",
        "schema_version": version,
        "anki_url": target_rows[0]["base_url"] if target_rows else None,
        "anki_targets": len(target_rows),
    }
