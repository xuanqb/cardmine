"""同步前备份 Anki 集合与媒体，使用文件快照与媒体硬链接快照。"""

import os
import shutil
from datetime import datetime
from pathlib import Path

from .. import config


def _backup_collection_file(source: Path, dest: Path) -> None:
    """按文件复制集合主库与 WAL/SHM 边车。

    Anki 后端以 EXCLUSIVE 锁持有集合，外部 SQLite 连接即使成功，
    Connection.backup() 也可能无限期阻塞且不抛出异常，因此不做在线备份。
    三个文件必须一起复制，单独复制主库会丢失尚未 checkpoint 的数据。
    """
    shutil.copy2(source, dest)
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if not sidecar.exists():
            continue
        try:
            shutil.copy2(sidecar, dest.with_name(dest.name + suffix))
        except FileNotFoundError:
            # 备份期间 Anki 可能轮换/删除边车文件
            continue


def _snapshot_media(source: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in source.iterdir():
        if not item.is_file() or item.name.endswith(("-wal", "-shm")):
            continue
        target = dest / item.name
        try:
            try:
                os.link(item, target)
            except OSError:
                shutil.copy2(item, target)
        except FileNotFoundError:
            # 备份期间 Anki 可能删除或替换媒体文件
            continue
        count += 1
    return count


def backup_anki(keep: int = 7) -> dict:
    source = config.ANKI_DATA_DIR / "collection.anki2"
    if not source.exists():
        return {"skipped": "Anki 集合文件不存在，可能尚未初始化"}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = config.BACKUP_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)

    try:
        _backup_collection_file(source, dest / "collection.anki2")
        if (dest / "collection.anki2").stat().st_size == 0:
            raise RuntimeError("Anki 集合备份为空，已中止同步")

        media_source = source.parent / "collection.media"
        media_count = (
            _snapshot_media(media_source, dest / "collection.media")
            if media_source.exists()
            else 0
        )
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise

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
