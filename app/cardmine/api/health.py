from fastapi import APIRouter

from .. import config, db

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict:
    conn = db.connect()
    try:
        conn.execute("SELECT 1").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    return {
        "status": "ok",
        "app_version": config.APP_VERSION,
        "db": "ok",
        "schema_version": version,
        "anki_url": config.ANKI_CONNECT_URL,
    }
