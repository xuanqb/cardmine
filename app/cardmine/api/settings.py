import json
from contextlib import closing
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import connect

router = APIRouter(prefix="/api", tags=["settings"])

DEFAULT_SETTINGS: dict[str, Any] = {
    "rss_url": "",
    "fetch_interval_minutes": 60,
    "ai_base_url": "",
    "ai_api_key": "",
    "ai_chat_model": "",
    "ai_embedding_model": "",
    "ai_embedding_dim": 1024,
    "dup_high_threshold": 0.92,
    "dup_low_threshold": 0.82,
    "anki_url": "http://anki-connect-server:8765/api",
    "anki_model": "Basic",
    "anki_sync_enabled": True,
    "anki_conflict_guard": True,
}


class SettingsIn(BaseModel):
    rss_url: str | None = None
    fetch_interval_minutes: int | None = Field(None, ge=5)
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_chat_model: str | None = None
    ai_embedding_model: str | None = None
    ai_embedding_dim: int | None = Field(None, ge=1)
    dup_high_threshold: float | None = Field(None, ge=0, le=1)
    dup_low_threshold: float | None = Field(None, ge=0, le=1)
    anki_url: str | None = None
    anki_model: str | None = None
    anki_sync_enabled: bool | None = None
    anki_conflict_guard: bool | None = None


def load_settings() -> dict[str, Any]:
    values = dict(DEFAULT_SETTINGS)
    with closing(connect()) as conn:
        rows = conn.execute("SELECT key, value FROM setting").fetchall()
    for row in rows:
        if row["key"] not in values:
            continue
        try:
            values[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            continue
    return values


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return load_settings()


@router.put("/settings")
def update_settings(payload: SettingsIn) -> dict[str, Any]:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)

    merged = {**load_settings(), **updates}
    if merged["dup_high_threshold"] <= merged["dup_low_threshold"]:
        raise HTTPException(status_code=400, detail="去重高阈值必须大于低阈值")

    if updates:
        with closing(connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO setting(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [
                    (key, json.dumps(value, ensure_ascii=False))
                    for key, value in updates.items()
                ],
            )

    return load_settings()
