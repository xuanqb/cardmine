"""同步前备份 Anki 集合与媒体，使用 SQLite 在线备份与媒体硬链接快照。"""

import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .. import config


def _backup_collection_file(source: Path, dest: Path) -> None:
    try:
        with closing(
            sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        ) as src, closing(sqlite3.connect(dest)) as out:
            src.backup(out)
    except sqlite3.Error:
        # WAL 只读恢复失败时退化为文件复制（连同 -wal/-shm）
        shutil.copy2(source, dest)
        for suffix in ("-wal", "-shm"):
            sidecar = source.with_name(source.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, dest.with_name(dest.name + suffix))


def _snapshot_media(source: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in source.iterdir():
        if not item.is_file() or item.name.endswith(("-wal", "-shm")):
            continue
        target = dest / item.name
        try:
            os.link(item, target)
        except OSError:
            shutil.copy2(item, target)
        count += 1
    return count


def backup_anki(keep: int = 7) -> dict:
    source = config.ANKI_DATA_DIR / "collection.anki2"
    if not source.exists():
        return {"skipped": "Anki 集合文件不存在，可能尚未初始化"}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = config.BACKUP_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)

    _backup_collection_file(source, dest / "collection.anki2")

    media_source = source.parent / "collection.media"
    media_count = (
        _snapshot_media(media_source, dest / "collection.media")
        if media_source.exists()
        else 0
    )

    backups = sorted(
        (path for path in config.BACKUP_DIR.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )
    for stale in backups[:-keep]:
        shutil.rmtree(stale, ignore_errors=True)

    return {
        "path": str(dest),
        "media": media_count,
        "collection_bytes": (dest / "collection.anki2").stat().st_size,
    }
