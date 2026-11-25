"""媒体上传与无引用文件清理。"""

import hashlib
import mimetypes
import re
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from .. import config
from ..db import connect, utc_now
from ..export import local_media_refs

router = APIRouter(prefix="/api/media", tags=["media"])

ALLOWED_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp3",
    ".wav",
    ".ogg",
    ".m4a",
}


def _safe_name(value: str) -> str:
    name = value.replace("\\", "/").split("/")[-1].strip()
    return re.split(r"[?#]", name, maxsplit=1)[0].strip()


def _references(conn) -> set[str]:
    refs: set[str] = set()
    for row in conn.execute(
        "SELECT front_md, back_md, ai_back_md FROM knowledge_point"
    ):
        refs |= local_media_refs(row["front_md"], row["back_md"], row["ai_back_md"])
    for row in conn.execute(
        "SELECT system_front, system_back, target_front, target_back "
        "FROM sync_conflict WHERE status = 'pending'"
    ):
        refs |= local_media_refs(
            row["system_front"],
            row["system_back"],
            row["target_front"],
            row["target_back"],
        )
    for row in conn.execute("SELECT front_md FROM kp_alias"):
        refs |= local_media_refs(row["front_md"])
    for row in conn.execute("SELECT raw_html, clean_md FROM post"):
        refs |= local_media_refs(row["raw_html"], row["clean_md"])
    for row in conn.execute("SELECT context_md FROM mention"):
        refs |= local_media_refs(row["context_md"])
    for row in conn.execute("SELECT content FROM ai_message"):
        refs |= local_media_refs(row["content"])
    return refs


def _orphan_files(conn) -> list[dict]:
    if not config.MEDIA_DIR.exists():
        return []
    refs = _references(conn)
    items = []
    for path in sorted(config.MEDIA_DIR.iterdir()):
        if not path.is_file() or path.name in refs:
            continue
        items.append({"filename": path.name, "size": path.stat().st_size})
    return items


@router.get("/orphans")
def list_orphans() -> dict:
    with closing(connect()) as conn:
        items = _orphan_files(conn)
    return {
        "count": len(items),
        "size": sum(item["size"] for item in items),
        "items": items[:200],
        "truncated": len(items) > 200,
    }


class CleanupIn(BaseModel):
    confirm: bool = False


@router.post("/cleanup")
def cleanup(payload: CleanupIn) -> dict:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="需要确认后才能删除")
    deleted: list[str] = []
    failed: list[str] = []
    with closing(connect()) as conn:
        items = _orphan_files(conn)
        with conn:
            for item in items:
                path = config.MEDIA_DIR / item["filename"]
                try:
                    path.unlink()
                except OSError as exc:
                    failed.append(f"{item['filename']}: {exc}")
                    continue
                conn.execute(
                    "DELETE FROM media WHERE filename = ?", (item["filename"],)
                )
                deleted.append(item["filename"])
    deleted_set = set(deleted)
    return {
        "deleted": len(deleted),
        "failed": failed,
        "size": sum(
            item["size"] for item in items if item["filename"] in deleted_set
        ),
    }


@router.post("/upload")
async def upload_media(
    filename: str = Query(..., max_length=255),
    file: UploadFile = File(...),
) -> dict:
    name = _safe_name(filename)
    extension = Path(name).suffix.lower()
    if not name or extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="仅支持 png/jpg/gif/webp 图片和 mp3/wav/ogg/m4a 音频",
        )

    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.MEDIA_DIR / name
    if dest.exists():
        raise HTTPException(status_code=409, detail="同名媒体已存在，不会覆盖")

    temp = dest.with_name(f".{dest.name}.upload")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(temp, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_MEDIA_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="媒体文件超过 20MB")
                digest.update(chunk)
                out.write(chunk)
        temp.replace(dest)
    except HTTPException:
        temp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写入媒体失败：{exc}") from exc

    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO media(filename, sha256, size, mime, path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(filename) DO UPDATE SET sha256 = excluded.sha256, "
            "size = excluded.size, mime = excluded.mime, path = excluded.path",
            (name, digest.hexdigest(), size, mime, name, utc_now()),
        )
    return {"filename": name, "size": size}
