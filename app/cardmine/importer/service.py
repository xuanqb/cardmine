"""apkg 导入：建目录树、卡片幂等入库、媒体落盘。"""

import mimetypes
import time
import zipfile
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .. import config, groups
from ..db import connect, utc_now
from .apkg import Package, read_package, write_media_file
from .textfmt import anki_to_markdown, media_refs, normalize_front

Progress = Callable[[str, int, int], None]


@dataclass
class ImportReport:
    cards_total: int = 0
    cards_new: int = 0
    cards_filled: int = 0
    cards_skipped: int = 0
    modules: int = 0
    media: int = 0
    media_missing: list[str] = field(default_factory=list)
    skipped_models: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _ensure_module(conn, path: str, cache: dict[str, int], created_by="apkg") -> int | None:
    path = path.strip("/")
    if not path:
        return None

    parent_id = None
    current = ""
    for part in path.split("/"):
        current = f"{current}/{part}" if current else part
        module_id = cache.get(current)
        if module_id is None:
            cursor = conn.execute(
                "INSERT INTO module(parent_id, name, path, created_by) VALUES (?, ?, ?, ?)",
                (parent_id, part, current, created_by),
            )
            module_id = cursor.lastrowid
            cache[current] = module_id
        parent_id = module_id
    return cache[path]


def _safe_filename(name: str) -> str:
    return name.replace("\\", "/").split("/")[-1].strip()


def run_import(apkg_path: str, progress: Progress | None = None) -> ImportReport:
    notify = progress or (lambda phase, done, total: None)
    started = time.monotonic()
    report = ImportReport()

    notify("解析安装包", 0, 1)
    package = read_package(apkg_path)
    try:
        notify("解析安装包", 1, 1)
        report = _import_package(package, notify)
    finally:
        package.cleanup()

    report.elapsed_ms = int((time.monotonic() - started) * 1000)
    return report


def _import_package(package: Package, notify: Progress) -> ImportReport:
    report = ImportReport()
    total_notes = len(package.notes)
    notify("读取卡片", 0, total_notes)

    missing_refs: set[str] = set()

    with closing(connect()) as conn, conn:
        cache = {
            row["path"]: row["id"] for row in conn.execute("SELECT id, path FROM module")
        }
        modules_before = len(cache)

        for deck_name in package.decks.values():
            _ensure_module(conn, deck_name.replace("\x1f", "/"), cache)
        report.modules = len(cache) - modules_before
        path_group = groups.path_group_map(conn)

        existing = {
            (row["group_id"], row["front_norm"]): (row["id"], row["back_md"])
            for row in conn.execute(
                "SELECT id, group_id, front_norm, back_md FROM knowledge_point"
            )
        }
        now = utc_now()

        for seq, note in enumerate(package.notes, start=1):
            model = package.models.get(note.model_id)
            if not model or "Front" not in model.fields or "Back" not in model.fields:
                name = model.name if model else f"model-{note.model_id}"
                report.skipped_models[name] = report.skipped_models.get(name, 0) + 1
                notify("读取卡片", seq, total_notes)
                continue

            front_index = model.fields.index("Front")
            back_index = model.fields.index("Back")
            front_raw = note.fields[front_index] if front_index < len(note.fields) else ""
            back_raw = note.fields[back_index] if back_index < len(note.fields) else ""

            missing_refs |= media_refs(front_raw, back_raw)

            front_md = anki_to_markdown(front_raw)
            back_md = anki_to_markdown(back_raw)
            front_norm = normalize_front(front_md)
            if not front_norm:
                report.errors.append(f"note {note.id}: 正面为空，已跳过")
                notify("读取卡片", seq, total_notes)
                continue

            report.cards_total += 1
            module_path = (note.deck or "").replace("\x1f", "/")
            module_id = cache.get(module_path)
            group_id = path_group.get(module_path)

            key = (group_id, front_norm)
            if key in existing:
                report.cards_skipped += 1
                kp_id, current_back = existing[key]
                if not current_back.strip() and back_md.strip():
                    conn.execute(
                        "UPDATE knowledge_point SET back_md = ?, status = 'ready', "
                        "updated_at = ? WHERE id = ?",
                        (back_md, now, kp_id),
                    )
                    existing[key] = (kp_id, back_md)
                    report.cards_filled += 1
            else:
                cursor = conn.execute(
                    "INSERT INTO knowledge_point(front_md, back_md, front_norm, module_id, "
                    "group_id, status, origin, created_at, updated_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, 'apkg', ?, ?)",
                    (
                        front_md,
                        back_md,
                        front_norm,
                        module_id,
                        group_id,
                        "ready" if back_md.strip() else "draft",
                        now,
                        now,
                    ),
                )
                existing[key] = (cursor.lastrowid, back_md)
                report.cards_new += 1

            notify("读取卡片", seq, total_notes)

        media_names = {
            name for name in package.media.values() if _safe_filename(name)
        }
        report.media_missing = sorted(missing_refs - media_names)

        total_media = len(package.media)
        notify("导入媒体", 0, total_media)

        with zipfile.ZipFile(package.path) as zf:
            seen: set[str] = set()
            total_bytes = 0
            for order, (index, name) in enumerate(sorted(package.media.items()), start=1):
                filename = _safe_filename(name)
                try:
                    if not filename or filename in seen:
                        continue
                    seen.add(filename)
                    dest = config.MEDIA_DIR / filename
                    sha256, size = write_media_file(zf, index, dest)
                    total_bytes += size
                    if total_bytes > config.MAX_IMPORT_MEDIA_BYTES:
                        dest.unlink(missing_ok=True)
                        report.errors.append(
                            "媒体总大小超过 4GB，已停止导入剩余媒体"
                        )
                        break
                    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    conn.execute(
                        "INSERT INTO media(filename, sha256, size, mime, path, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(filename) DO UPDATE SET sha256 = excluded.sha256, "
                        "size = excluded.size, mime = excluded.mime, path = excluded.path",
                        (filename, sha256, size, mime, filename, utc_now()),
                    )
                    report.media += 1
                except Exception as exc:  # 单个媒体失败不阻断整体导入
                    report.errors.append(f"media {name}: {exc}")
                notify("导入媒体", order, total_media)

    return report
