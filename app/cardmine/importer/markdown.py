"""Markdown 题库导入：按 ## 分章节、### 分题目，题干作正面、正文作背面。"""

import hashlib
import io
import mimetypes
import re
import time
import zipfile
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from .. import config
from ..db import connect, utc_now
from .textfmt import normalize_front, normalize_media_ref

_NUMBER_PREFIX = re.compile(
    r"^(?:[一二三四五六七八九十百千]+(?:[\-－]\d+|\d+)?|Q\s*\d+|\d+)\s*[、.．。：:]\s*"
)
_ORDERED_ITEM = re.compile(r"^\s{0,3}\d+\s*[.、．]\s*")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)([^)]*)\)")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _is_remote(src: str) -> bool:
    return src.startswith(("http://", "https://", "//", "data:"))


@dataclass
class MarkdownImportReport:
    dry_run: bool = False
    cards_total: int = 0
    cards_new: int = 0
    cards_filled: int = 0
    cards_overwritten: int = 0
    cards_skipped: int = 0
    skipped_empty_front: int = 0
    modules: int = 0
    media: int = 0
    media_missing: list[str] = field(default_factory=list)
    media_conflicts: list[str] = field(default_factory=list)
    samples: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Question:
    section: str
    front: str
    back: str


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别 Markdown 编码（仅支持 UTF-8 / GB18030）")


def _clean_title(value: str) -> str:
    title = _NUMBER_PREFIX.sub("", value.strip(), count=1)
    title = re.sub(r"`+", "", title)
    title = re.sub(r"\*\*|__|~~", "", title)
    return re.sub(r"\s+", " ", title).strip()


def _dedent(body: list[str]) -> str:
    lines = list(body)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    cut = min(indents) if indents else 0
    if cut:
        lines = [line[cut:] if line.strip() else "" for line in lines]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


def _normalize_images(text: str) -> str:
    """把本地图片引用收敛成扁平文件名，预览会按 /media/<name> 解析。"""

    def repl(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        if _is_remote(src):
            return match.group(0)
        name = PurePosixPath(
            normalize_media_ref(src).replace("\\", "/")
        ).name
        if not name:
            return match.group(0)
        return f"![{alt}]({name})"

    return _MD_IMAGE.sub(repl, text)


def _image_names(text: str) -> list[str]:
    names = []
    for match in _MD_IMAGE.finditer(text):
        if _is_remote(match.group(2)):
            continue
        name = PurePosixPath(
            normalize_media_ref(match.group(2)).replace("\\", "/")
        ).name
        if name and name not in names:
            names.append(name)
    return names


def _looks_like_misplaced_answers(back: str, titles: list[str]) -> bool:
    """识别「题干只有标题、答案集中堆在最后一题」的错位结构。"""
    first = next((line for line in back.splitlines() if line.strip()), "")
    if not _ORDERED_ITEM.match(first):
        return False
    compact = re.sub(r"\s+", "", back).casefold()
    hits = sum(1 for title in titles if title and re.sub(r"\s+", "", title).casefold() in compact)
    return hits >= 2


def parse_markdown(text: str) -> tuple[list[_Question], int]:
    """解析 Markdown，返回题目列表与空标题数量。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    raw: list[tuple[str, str, list[str]]] = []
    section = ""
    current: tuple[str, str, list[str]] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            raw.append(current)
            current = None

    for line in lines:
        if line.startswith("## "):
            flush()
            section = line[3:].strip()
            continue
        if line.startswith("### "):
            flush()
            current = (section, line[4:].strip(), [])
            continue
        if current is not None:
            current[2].append(line)
    flush()

    questions: list[_Question] = []
    empty_front = 0
    for section_name, raw_title, body in raw:
        front = _clean_title(raw_title)
        if not front:
            empty_front += 1
            continue
        questions.append(
            _Question(
                section=section_name,
                front=front,
                back=_dedent(body),
            )
        )

    by_section: dict[str, list[str]] = {}
    for question in questions:
        by_section.setdefault(question.section, []).append(question.front)
    for question in questions:
        if "算法" in question.section and _looks_like_misplaced_answers(
            question.back, by_section[question.section]
        ):
            question.back = ""

    for question in questions:
        question.front = _normalize_images(question.front)
        question.back = _normalize_images(question.back)
    return questions, empty_front


def _ensure_module(
    conn,
    path: str,
    cache: dict[str, int],
    created_by: str = "manual",
    sort: int | None = None,
) -> int:
    parent_id = None
    current = ""
    for part in path.split("/"):
        current = f"{current}/{part}" if current else part
        module_id = cache.get(current)
        if module_id is None:
            if sort is None:
                order = conn.execute(
                    "SELECT COALESCE(MAX(sort), 0) + 1 FROM module WHERE parent_id IS ?",
                    (parent_id,),
                ).fetchone()[0]
            else:
                order = sort
            cursor = conn.execute(
                "INSERT INTO module(parent_id, name, path, sort, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (parent_id, part, current, order, created_by),
            )
            module_id = cursor.lastrowid
            cache[current] = module_id
        parent_id = module_id
    return cache[path]


def _zip_image_index(zf: zipfile.ZipFile) -> tuple[dict[str, zipfile.ZipInfo], dict[str, zipfile.ZipInfo]]:
    by_path: dict[str, zipfile.ZipInfo] = {}
    by_name: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if info.is_dir() or "__MACOSX" in info.filename:
            continue
        path = PurePosixPath(info.filename.replace("\\", "/"))
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        normalized = str(path)
        while normalized.startswith("./"):
            normalized = normalized[2:]
        by_path.setdefault(normalized, info)
        by_name.setdefault(path.name, info)
    return by_path, by_name


def _store_image(
    conn,
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    name: str,
    report: MarkdownImportReport,
    dry_run: bool = False,
) -> None:
    if info.file_size > config.MAX_MEDIA_UPLOAD_BYTES:
        report.errors.append(f"图片 {name} 超过 20MB，已跳过")
        return

    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.MEDIA_DIR / name
    try:
        data = zf.read(info)
    except (OSError, zipfile.BadZipFile) as exc:
        report.errors.append(f"图片 {name} 读取失败：{exc}")
        return
    digest = hashlib.sha256(data).hexdigest()
    created = False

    if dest.exists():
        existing = hashlib.sha256(dest.read_bytes()).hexdigest()
        if existing != digest:
            report.media_conflicts.append(name)
            return
    else:
        if dry_run:
            report.media += 1
            return
        try:
            dest.write_bytes(data)
        except OSError as exc:
            report.errors.append(f"图片 {name} 写入失败：{exc}")
            return
        created = True

    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    conn.execute(
        "INSERT INTO media(filename, sha256, size, mime, path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(filename) DO UPDATE SET sha256 = excluded.sha256, "
        "size = excluded.size, mime = excluded.mime, path = excluded.path",
        (name, digest, len(data), mime, name, utc_now()),
    )
    if created:
        report.media += 1


def _import_questions(
    questions: list[_Question],
    module_path: str,
    zf: zipfile.ZipFile | None,
    report: MarkdownImportReport,
    dry_run: bool = False,
    overwrite: bool = False,
) -> None:
    by_path, by_name = _zip_image_index(zf) if zf else ({}, {})

    with closing(connect()) as conn:
        cache = {
            row["path"]: row["id"] for row in conn.execute("SELECT id, path FROM module")
        }
        modules_before = len(cache)
        root_id = _ensure_module(conn, module_path, cache)
        child_sort: dict[str, int] = {}
        for question in questions:
            section = question.section.strip()
            if not section:
                continue
            if section not in child_sort:
                child_sort[section] = len(child_sort)
                _ensure_module(
                    conn,
                    f"{module_path}/{section}",
                    cache,
                    sort=child_sort[section],
                )
        report.modules = len(cache) - modules_before

        existing = {
            row["front_norm"]: (row["id"], row["back_md"], row["origin"])
            for row in conn.execute(
                "SELECT id, front_norm, back_md, origin FROM knowledge_point"
            )
        }
        now = utc_now()
        media_budget = config.MAX_MARKDOWN_MEDIA_BYTES
        seen_images: set[str] = set()

        for question in questions:
            section = question.section.strip()
            module_id = cache.get(f"{module_path}/{section}", root_id) if section else root_id

            for name in _image_names(f"{question.front}\n{question.back}"):
                if name in seen_images:
                    continue
                seen_images.add(name)
                info = by_path.get(name) or by_name.get(name)
                if info is None:
                    if not (config.MEDIA_DIR / name).exists() and name not in report.media_missing:
                        report.media_missing.append(name)
                    continue
                if info.file_size > media_budget:
                    report.errors.append(f"图片 {name} 超出本次导入图片总量上限，已跳过")
                    continue
                media_budget -= info.file_size
                _store_image(conn, zf, info, name, report, dry_run=dry_run)

            front_norm = normalize_front(question.front)
            if front_norm in existing:
                kp_id, current_back, origin = existing[front_norm]
                if not current_back.strip() and question.back.strip():
                    conn.execute(
                        "UPDATE knowledge_point SET back_md = ?, updated_at = ? WHERE id = ?",
                        (question.back, now, kp_id),
                    )
                    existing[front_norm] = (kp_id, question.back, origin)
                    report.cards_filled += 1
                elif (
                    overwrite
                    and origin == "manual"
                    and question.back.strip()
                    and question.back != current_back
                ):
                    conn.execute(
                        "UPDATE knowledge_point SET back_md = ?, updated_at = ? WHERE id = ?",
                        (question.back, now, kp_id),
                    )
                    existing[front_norm] = (kp_id, question.back, origin)
                    report.cards_overwritten += 1
                else:
                    report.cards_skipped += 1
                continue

            cursor = conn.execute(
                "INSERT INTO knowledge_point(front_md, back_md, front_norm, module_id, "
                "status, origin, created_at, updated_at) VALUES (?, ?, ?, ?, 'draft', "
                "'manual', ?, ?)",
                (question.front, question.back, front_norm, module_id, now, now),
            )
            existing[front_norm] = (cursor.lastrowid, question.back, "manual")
            report.cards_new += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()


def run_markdown_import(
    filename: str,
    raw: bytes,
    module_path: str = "前端",
    dry_run: bool = False,
    overwrite: bool = False,
) -> MarkdownImportReport:
    module_path = module_path.strip().strip("/")
    if not module_path:
        raise ValueError("模块名不能为空")
    if "/" in module_path:
        raise ValueError("模块名不能包含 /")

    started = time.monotonic()
    report = MarkdownImportReport(dry_run=dry_run)

    if Path(filename).suffix.lower() == ".zip":
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise ValueError("zip 文件已损坏或格式不正确") from exc
        with zf:
            markdown_entries = [
                info
                for info in zf.infolist()
                if not info.is_dir()
                and "__MACOSX" not in info.filename
                and not PurePosixPath(info.filename).name.startswith(".")
                and PurePosixPath(info.filename).suffix.lower() in (".md", ".markdown")
            ]
            if len(markdown_entries) != 1:
                raise ValueError("zip 中需要且只能有一个 Markdown 文件")
            questions, empty_front = parse_markdown(_decode(zf.read(markdown_entries[0])))
            report.cards_total = len(questions) + empty_front
            report.skipped_empty_front = empty_front
            report.samples = _samples(questions)
            _import_questions(
                questions, module_path, zf, report, dry_run=dry_run, overwrite=overwrite
            )
    else:
        questions, empty_front = parse_markdown(_decode(raw))
        report.cards_total = len(questions) + empty_front
        report.skipped_empty_front = empty_front
        report.samples = _samples(questions)
        _import_questions(
            questions, module_path, None, report, dry_run=dry_run, overwrite=overwrite
        )

    report.elapsed_ms = int((time.monotonic() - started) * 1000)
    return report


def _samples(questions: list[_Question], limit: int = 8) -> list[dict]:
    return [
        {
            "front": question.front,
            "back_length": len(question.back),
            "back_excerpt": question.back[:240],
            "has_images": "![" in question.back,
        }
        for question in questions[:limit]
    ]
