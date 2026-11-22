"""apkg / colpkg 读取：collection 数据库、模型、牌组、媒体映射。"""

import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import zstandard

from .. import config

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class ApkgError(Exception):
    pass


@dataclass
class Model:
    name: str
    fields: list[str]


@dataclass
class Note:
    id: int
    model_id: int
    fields: list[str]
    deck: str | None


@dataclass
class Package:
    path: str
    workdir: str
    notes: list[Note]
    models: dict[int, Model]
    decks: dict[int, str]
    media: dict[int, str]

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def _unicase(left: str | None, right: str | None) -> int:
    a = (left or "").casefold()
    b = (right or "").casefold()
    return (a > b) - (a < b)


def _read_db(zf: zipfile.ZipFile, workdir: str) -> str:
    names = set(zf.namelist())
    for entry in ("collection.anki21b", "collection.anki21", "collection.anki2"):
        if entry not in names:
            continue
        dest = str(Path(workdir) / "collection.sqlite")
        with zf.open(entry) as src, open(dest, "wb") as out:
            if entry == "collection.anki21b":
                with zstandard.ZstdDecompressor().stream_reader(src) as reader:
                    shutil.copyfileobj(reader, out)
            else:
                shutil.copyfileobj(src, out)
        return dest
    raise ApkgError("安装包内未找到 collection.anki2 / anki21 / anki21b")


def _read_models(conn: sqlite3.Connection) -> dict[int, Model]:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }

    if "notetypes" in tables and "fields" in tables:
        names = dict(conn.execute("SELECT id, name FROM notetypes"))
        grouped: dict[int, list[tuple[int, str]]] = {}
        for ntid, order, field_name in conn.execute(
            "SELECT ntid, ord, name FROM fields"
        ):
            grouped.setdefault(ntid, []).append((order, field_name))
        return {
            mid: Model(names.get(mid, f"model-{mid}"), [n for _, n in sorted(items)])
            for mid, items in grouped.items()
        }

    row = conn.execute("SELECT models FROM col").fetchone()
    if not row:
        return {}
    models = json.loads(row[0])
    return {
        int(mid): Model(
            model.get("name", f"model-{mid}"),
            [field["name"] for field in model.get("flds", [])],
        )
        for mid, model in models.items()
    }


def _read_decks(conn: sqlite3.Connection) -> dict[int, str]:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "decks" in tables:
        return {
            deck_id: name for deck_id, name in conn.execute("SELECT id, name FROM decks")
        }

    row = conn.execute("SELECT decks FROM col").fetchone()
    if not row:
        return {}
    decks = json.loads(row[0])
    return {int(deck_id): deck["name"] for deck_id, deck in decks.items()}


def _varint(buf: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7


def _parse_media_entry(buf: bytes) -> bytes | None:
    index = 0
    name = None
    while index < len(buf):
        key, index = _varint(buf, index)
        field, wire = key >> 3, key & 7
        if wire == 2:
            length, index = _varint(buf, index)
            value = buf[index : index + length]
            index += length
            if field == 1:
                name = value
        elif wire == 0:
            _, index = _varint(buf, index)
        elif wire == 5:
            index += 4
        elif wire == 1:
            index += 8
        else:
            break
    return name


def _parse_media_protobuf(buf: bytes) -> dict[int, str]:
    media: dict[int, str] = {}
    entries: list[bytes | None] = []
    position = 0
    while position < len(buf):
        key, position = _varint(buf, position)
        field, wire = key >> 3, key & 7
        if field == 1 and wire == 2:
            length, position = _varint(buf, position)
            entry = buf[position : position + length]
            position += length
            entries.append(_parse_media_entry(entry))
        elif wire == 2:
            length, position = _varint(buf, position)
            position += length
        elif wire == 0:
            _, position = _varint(buf, position)
        elif wire == 5:
            position += 4
        elif wire == 1:
            position += 8
        else:
            break

    for index, name in enumerate(entries):
        if name:
            media[index] = name.decode("utf-8")
    return media


def _read_media(zf: zipfile.ZipFile) -> dict[int, str]:
    if "media" not in zf.namelist():
        return {}
    raw = zf.read("media")
    if raw[:4] == ZSTD_MAGIC:
        raw = zstandard.ZstdDecompressor().decompress(
            raw, max_output_size=64 * 1024 * 1024
        )
    if raw.lstrip().startswith(b"{"):
        data = json.loads(raw.decode("utf-8"))
        return {
            int(index): name for index, name in data.items() if isinstance(name, str)
        }
    return _parse_media_protobuf(raw)


def read_package(path: str) -> Package:
    workdir = tempfile.mkdtemp(prefix="cardmine-apkg-")
    try:
        with zipfile.ZipFile(path) as zf:
            db_path = _read_db(zf, workdir)
            media = _read_media(zf)

        conn = sqlite3.connect(db_path)
        conn.create_collation("unicase", _unicase)
        try:
            models = _read_models(conn)
            decks = _read_decks(conn)
            note_decks: dict[int, int] = {}
            for nid, did in conn.execute("SELECT nid, did FROM cards ORDER BY id"):
                note_decks.setdefault(nid, did)
            notes = [
                Note(
                    id=note_id,
                    model_id=model_id,
                    fields=flds.split("\x1f"),
                    deck=decks.get(note_decks.get(note_id)),
                )
                for note_id, model_id, flds in conn.execute(
                    "SELECT id, mid, flds FROM notes ORDER BY id"
                )
            ]
        finally:
            conn.close()
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    return Package(
        path=path,
        workdir=workdir,
        notes=notes,
        models=models,
        decks=decks,
        media=media,
    )


def write_media_file(zf: zipfile.ZipFile, index: int, dest: Path) -> tuple[str, int]:
    """把 zip 中索引对应的媒体解压后写入 dest，返回 (sha256, 字节数)。"""
    raw = zf.read(str(index))
    if raw[:4] == ZSTD_MAGIC:
        source = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw))
    else:
        source = io.BytesIO(raw)

    import hashlib

    digest = hashlib.sha256()
    size = 0
    try:
        with source, open(dest, "wb") as out:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                if size > config.MAX_MEDIA_BYTES:
                    raise ValueError("单个媒体文件超过 512MB")
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return digest.hexdigest(), size
