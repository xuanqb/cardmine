"""多目标对等同步：每个目标独立笔记映射、三向合并、整卡冲突与删除传播。"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import re
from contextlib import closing
from typing import Callable

from .. import config, groups
from ..db import connect, utc_now
from ..export import local_media_refs
from ..importer.textfmt import (
    anki_to_markdown,
    normalize_front,
    normalize_media_ref,
)
from . import targets
from .backup import backup_anki
from .client import AnkiClient, AnkiError

Progress = Callable[[str, int, int], None]
AUDIT_LIMIT = 500
MARKDOWN_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
REMOTE_PREFIXES = ("http://", "https://", "//", "/")

FIELD_NAMES = {"front": "Front", "back": "Back"}
_HTML_HINT_RE = re.compile(
    r"<\s*(?:img|br|pre|code|div|span|p|b|i|u|strong|em|ul|ol|li|table|tr|td|a|font)\b",
    re.I,
)
_FIELD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)\s*\)")
_FIELD_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")

_UNSET = object()


def _notify(progress: Progress | None, phase: str, done: int, total: int) -> None:
    if progress:
        progress(phase, done, total)


def _search_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _deck_name(module_path: str | None) -> str:
    return (module_path or "Default").replace("/", "::")


def _system_deck(conn, kp_id: int) -> str:
    row = conn.execute(
        "SELECT m.path FROM knowledge_point kp "
        "LEFT JOIN module m ON m.id = kp.module_id WHERE kp.id = ?",
        (kp_id,),
    ).fetchone()
    return _deck_name(row["path"] if row else None)


def _content_hash(front: str, back: str, deck: str, media: set[str]) -> str:
    raw = json.dumps(
        [front, back, deck, sorted(media)], ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_local_media(name: str) -> bool:
    return not name.startswith(REMOTE_PREFIXES)


def _normalize_media_links(text: str) -> str:
    def image(match: re.Match) -> str:
        return f"![{match.group(1)}]({normalize_media_ref(match.group(2))})"

    def sound(match: re.Match) -> str:
        return f"[sound:{normalize_media_ref(match.group(1))}]"

    return _FIELD_SOUND_RE.sub(sound, _FIELD_IMG_RE.sub(image, text))


def normalize_markdown(text: str) -> str:
    """字段规范化：统一换行、实体、行尾空白与媒体引用，HTML 转 Markdown。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if _HTML_HINT_RE.search(text):
        text = anki_to_markdown(text)
    else:
        text = html.unescape(text)
    text = _normalize_media_links(text)
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def field_hash(text: str) -> str:
    return hashlib.sha256(normalize_markdown(text).encode("utf-8")).hexdigest()


def field_action(system_hash: str, target_hash: str, base_hash: str | None) -> str:
    """单字段三向判定：none/push/pull/same/conflict。"""
    if base_hash is None:
        return "same" if system_hash == target_hash else "conflict"
    system_changed = system_hash != base_hash
    target_changed = target_hash != base_hash
    if not system_changed and not target_changed:
        return "none"
    if system_changed and not target_changed:
        return "push"
    if not system_changed and target_changed:
        return "pull"
    return "same" if system_hash == target_hash else "conflict"


def _markdown_media(*texts: str) -> set[str]:
    refs: set[str] = set()
    for text in texts:
        if not text:
            continue
        refs.update(MARKDOWN_IMG_RE.findall(text))
        refs.update(SOUND_RE.findall(text))
    return refs


def _system_content_hash(front: str, back: str, deck: str) -> str:
    return _content_hash(front, back, deck, _markdown_media(front, back))


def _plain_text(text: str) -> str:
    text = MARKDOWN_IMG_RE.sub("", text)
    text = re.sub(r"[`*_#>\[\]()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _anki_field(info: dict | None, key: str) -> str:
    if not info:
        return ""
    fields = info.get("fields") or {}
    return (fields.get(key) or {}).get("value", "") or ""


def _target_field_markdown(info: dict | None, key: str) -> str:
    return normalize_markdown(anki_to_markdown(_anki_field(info, key)))


def _default_error(conn, kp_id: int, target_id: int, message: str) -> None:
    conn.execute(
        "UPDATE anki_sync SET last_error = ? WHERE kp_id = ? AND target_id = ?",
        (message, kp_id, target_id),
    )


def _upsert_sync(
    conn,
    kp_id: int,
    target_id: int,
    note_id: int | None,
    deck: str | None,
    front_base: str | None,
    back_base: str | None,
    combined=_UNSET,
    pushed_at=_UNSET,
    detached: bool = False,
    touch: bool = True,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO anki_sync
          (kp_id, target_id, anki_note_id, pushed_hash, pushed_at, pushed_deck,
           pushed_front_hash, pushed_back_hash, last_error, detached, last_seen_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(kp_id, target_id) DO UPDATE SET
          anki_note_id = excluded.anki_note_id,
          pushed_deck = excluded.pushed_deck,
          pushed_front_hash = excluded.pushed_front_hash,
          pushed_back_hash = excluded.pushed_back_hash,
          last_error = NULL,
          detached = excluded.detached,
          last_seen_at = COALESCE(excluded.last_seen_at, anki_sync.last_seen_at)
        """,
        (
            kp_id,
            target_id,
            note_id,
            combined if combined is not _UNSET else None,
            pushed_at if pushed_at is not _UNSET else None,
            deck,
            front_base,
            back_base,
            1 if detached else 0,
            now if touch else None,
        ),
    )
    if combined is not _UNSET:
        conn.execute(
            "UPDATE anki_sync SET pushed_hash = ?, pushed_at = ? "
            "WHERE kp_id = ? AND target_id = ?",
            (
                combined,
                pushed_at if pushed_at is not _UNSET else now,
                kp_id,
                target_id,
            ),
        )


def _set_last_seen(conn, kp_id: int, target_id: int) -> None:
    conn.execute(
        "UPDATE anki_sync SET last_seen_at = ? WHERE kp_id = ? AND target_id = ?",
        (utc_now(), kp_id, target_id),
    )


def _record_flag(conn, kp_id: int, target_id: int, flag: int) -> None:
    conn.execute(
        "INSERT INTO anki_sync(kp_id, target_id, anki_flag) VALUES(?, ?, ?) "
        "ON CONFLICT(kp_id, target_id) DO UPDATE SET anki_flag = excluded.anki_flag",
        (kp_id, target_id, flag),
    )


def _load_rows(conn, target_id: int, statuses: tuple[str, ...]):
    placeholders = ",".join("?" for _ in statuses)
    return conn.execute(
        f"""
        SELECT kp.id, kp.front_md, kp.back_md, kp.front_norm, kp.status,
               kp.group_id, kp.module_id, m.path AS module_path,
               s.anki_note_id, s.pushed_hash, s.pushed_deck,
               s.pushed_front_hash, s.pushed_back_hash, s.detached,
               s.force_push, s.anki_flag, s.flag_clear_pending
        FROM knowledge_point kp
        JOIN group_target gt
          ON gt.group_id = kp.group_id AND gt.target_id = ?
        LEFT JOIN module m ON m.id = kp.module_id
        LEFT JOIN anki_sync s
          ON s.kp_id = kp.id AND s.target_id = ?
        WHERE kp.status IN ({placeholders})
        ORDER BY kp.id
        """,
        (target_id, target_id, *statuses),
    ).fetchall()


def _pending_conflict_cards(conn) -> set[int]:
    return {
        int(row["kp_id"])
        for row in conn.execute(
            "SELECT DISTINCT kp_id FROM sync_conflict WHERE status = 'pending'"
        )
    }


def _fetch_notes(
    client: AnkiClient,
    note_ids: set[int],
    progress: Progress | None = None,
    phase: str = "读取 Anki",
) -> dict[int, dict]:
    ordered = sorted(int(note) for note in note_ids)
    infos: dict[int, dict] = {}
    for offset in range(0, len(ordered), 500):
        chunk = ordered[offset : offset + 500]
        try:
            data = client.call("notesInfo", notes=chunk)
        except AnkiError as exc:
            raise AnkiError(f"读取 Anki 笔记失败：{exc}") from exc
        for info in data or []:
            if info and info.get("noteId"):
                infos[int(info["noteId"])] = info
        _notify(progress, phase, min(offset + 500, len(ordered)), len(ordered))
    return infos


def _note_decks(client: AnkiClient, infos: dict[int, dict]) -> dict[int, str]:
    """通过 notesInfo 的 cards 批量读取笔记当前牌组。"""
    card_note: dict[int, int] = {}
    for note_id, info in infos.items():
        for card_id in info.get("cards") or []:
            card_note[int(card_id)] = int(note_id)
    if not card_note:
        return {}
    try:
        cards = client.call("cardsInfo", cards=list(card_note)) or []
    except AnkiError:
        return {}
    decks: dict[int, str] = {}
    for card in cards:
        note_id = card_note.get(int(card.get("cardId") or 0))
        if note_id is not None and card.get("deckName"):
            decks.setdefault(note_id, card["deckName"])
    return decks


def _fetch_note_flags(
    client: AnkiClient,
    note_ids: set[int],
    progress: Progress | None = None,
) -> dict[int, tuple[int, list[int]]]:
    """返回 note_id -> (旗帜颜色, 带旗卡片)。基于搜索，不依赖 notesInfo 的 cards。"""
    _ = progress
    wanted = {int(note) for note in note_ids}
    found: dict[int, tuple[int, list[int]]] = {}
    if not wanted:
        return found
    for value in range(1, 8):
        try:
            cards = client.call("findCards", query=f"flag:{value}") or []
            if not cards:
                continue
            notes = client.call(
                "cardsToNotes", cards=[int(card) for card in cards]
            ) or []
        except AnkiError as exc:
            raise AnkiError(f"读取 Anki 旗帜失败：{exc}") from exc
        for card_id, note_id in zip(cards, notes):
            note_id = int(note_id)
            if note_id not in wanted:
                continue
            current = found.get(note_id)
            if current is None:
                found[note_id] = (value, [int(card_id)])
            else:
                found[note_id] = (max(current[0], value), [*current[1], int(card_id)])
    return found


def _ensure_deck(client: AnkiClient, decks: set[str], deck: str) -> None:
    if deck not in decks:
        client.call("createDeck", deck=deck)
        decks.add(deck)


def _add_note(
    client: AnkiClient, model_name: str, deck: str, front: str, back: str
) -> int | None:
    return client.call(
        "addNote",
        note={
            "deckName": deck,
            "modelName": model_name,
            "fields": {"Front": front, "Back": back},
            "tags": [],
            "options": {"allowDuplicate": False, "duplicateScope": "deck"},
        },
    )


def _move_to_deck(client: AnkiClient, note_id: int, deck: str) -> None:
    cards = client.call("findCards", query=f"nid:{note_id}")
    if cards:
        client.call("changeDeck", cards=cards, deck=deck)


def _find_note_by_front(client: AnkiClient, deck: str, front: str) -> int | None:
    """addNote 返回空（重复）时，按正面文本在牌组内找回已有笔记。"""
    query_text = _plain_text(front)[:60]
    if not query_text:
        return None
    try:
        found = client.call(
            "findNotes",
            query=f'deck:"{_search_escape(deck)}" "{_search_escape(query_text)}"',
        )
        if not found:
            return None
        infos = client.call("notesInfo", notes=found[:200])
    except AnkiError:
        return None

    target = normalize_front(front)
    for info in infos:
        fields = info.get("fields") or {}
        value = (fields.get("Front") or {}).get("value", "")
        if normalize_front(anki_to_markdown(value)) == target:
            return int(info["noteId"])
    return None


def _record_media(conn, name: str, raw: bytes) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    conn.execute(
        "INSERT INTO media(filename, sha256, size, mime, path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(filename) DO UPDATE SET sha256 = excluded.sha256, "
        "size = excluded.size, mime = excluded.mime, path = excluded.path",
        (name, digest, len(raw), mime, name, utc_now()),
    )


def _pull_media(conn, client: AnkiClient, refs: set[str]) -> list[str]:
    errors: list[str] = []
    for name in sorted(refs):
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            errors.append(f"媒体文件名非法: {name}")
            continue
        if (config.MEDIA_DIR / name).is_file():
            continue
        try:
            data = client.call("retrieveMediaFile", filename=name)
        except AnkiError as exc:
            errors.append(f"媒体 {name} 拉取失败: {exc}")
            continue
        if not data:
            errors.append(f"媒体 {name} 在 Anki 中不存在")
            continue
        try:
            raw = base64.b64decode(data)
        except (ValueError, TypeError):
            errors.append(f"媒体 {name} 数据损坏")
            continue
        (config.MEDIA_DIR / name).write_bytes(raw)
        _record_media(conn, name, raw)
    return errors


def _push_media(
    client: AnkiClient, refs: set[str]
) -> tuple[int, set[str], list[str]]:
    """上传缺失媒体；目标已有同名但内容不同则记为冲突，不覆盖。"""
    errors: list[str] = []
    conflicts: set[str] = set()
    if not refs:
        return 0, conflicts, errors
    try:
        remote = set(client.call("getMediaFilesNames", pattern="*") or [])
    except AnkiError as exc:
        return 0, set(refs), [f"无法读取目标媒体列表，已暂缓含媒体的卡片: {exc}"]

    uploaded = 0
    for name in sorted(refs):
        path = config.MEDIA_DIR / name
        if not path.is_file():
            errors.append(f"媒体缺失: {name}")
            continue
        raw = path.read_bytes()
        if name in remote:
            try:
                data = client.call("retrieveMediaFile", filename=name)
                existing = base64.b64decode(data) if data else b""
            except (AnkiError, ValueError, TypeError):
                existing = None
            if existing is not None and existing != raw:
                conflicts.add(name)
            continue
        try:
            data = base64.b64encode(raw).decode("ascii")
            client.call("storeMediaFile", filename=name, data=data)
            uploaded += 1
        except AnkiError as exc:
            errors.append(f"媒体 {name}: {exc}")
    return uploaded, conflicts, errors


_TEMPLATE_MEDIA_EXTS = {
    "js", "css", "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp",
    "mp3", "ogg", "wav", "m4a", "flac", "mp4", "webm",
    "woff", "woff2", "ttf", "otf",
}
_TEMPLATE_MEDIA_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']([^"']+)["']|url\(\s*["']?([^"')]+)["']?\s*\)""",
    re.IGNORECASE,
)
# 模板内联 JS 里常见 getScript("markdown-it.min.js") 这类动态加载
_QUOTED_MEDIA_RE = re.compile(
    r"""["']([^"'\s/\\]+\.(?:%s))["']"""
    % "|".join(sorted(_TEMPLATE_MEDIA_EXTS)),
    re.IGNORECASE,
)


def _template_media_refs(css: str, templates: dict) -> set[str]:
    """从模型 CSS 与卡片模板中提取本地媒体文件名。"""
    texts = [css or ""]
    for body in templates.values():
        texts.append(body.get("Front") or "")
        texts.append(body.get("Back") or "")
    refs: set[str] = set()

    def _add(raw: str) -> None:
        name = (raw or "").strip()
        if name.startswith("./"):
            name = name[2:]
        if not name or name.startswith(("http:", "https:", "data:", "//", "#")):
            return
        if "/" in name or "\\" in name or name in (".", ".."):
            return
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in _TEMPLATE_MEDIA_EXTS:
            refs.add(name)

    for text in texts:
        for match in _TEMPLATE_MEDIA_RE.finditer(text):
            _add(match.group(1) or match.group(2) or "")
        for match in _QUOTED_MEDIA_RE.finditer(text):
            _add(match.group(1))
    return refs


def _copy_template_media(
    source: AnkiClient, client: AnkiClient, refs: set[str]
) -> tuple[int, list[str]]:
    """把源目标媒体库中的模板依赖复制到目标，已存在则跳过。"""
    if not refs:
        return 0, []
    try:
        remote = set(client.call("getMediaFilesNames", pattern="*") or [])
    except AnkiError as exc:
        return 0, [f"无法读取目标媒体列表: {exc}"]
    copied = 0
    errors: list[str] = []
    for name in sorted(refs):
        if name in remote:
            continue
        try:
            data = source.call("retrieveMediaFile", filename=name)
        except AnkiError as exc:
            errors.append(f"媒体 {name} 读取失败: {exc}")
            continue
        if not data:
            errors.append(f"媒体 {name} 在源目标中不存在")
            continue
        try:
            client.call("storeMediaFile", filename=name, data=data)
            copied += 1
        except AnkiError as exc:
            errors.append(f"媒体 {name} 写入失败: {exc}")
    return copied, errors


def _front_collision(conn, kp_id: int, group_id: int | None, front_norm: str):
    return conn.execute(
        "SELECT id FROM knowledge_point WHERE front_norm = ? "
        "AND group_id IS ? AND id != ?",
        (front_norm, group_id, kp_id),
    ).fetchone()


def _record_conflict(
    conn,
    target_id: int,
    kp_id: int,
    note_id: int | None,
    kind: str,
    note: str,
    system_front: str,
    system_back: str,
    system_deck: str | None,
    target_front: str | None,
    target_back: str | None,
    target_deck: str | None,
    base_front: str | None,
    base_back: str | None,
) -> bool:
    """整卡冲突：每张卡最多一条待处理记录。返回是否新建。"""
    existing = conn.execute(
        "SELECT id FROM sync_conflict WHERE kp_id = ? AND status = 'pending'",
        (kp_id,),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """
        INSERT INTO sync_conflict
          (kp_id, target_id, anki_note_id, kind, system_front, system_back,
           system_deck, target_front, target_back, target_deck,
           base_front_hash, base_back_hash, note, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kp_id,
            target_id,
            note_id,
            kind,
            system_front,
            system_back,
            system_deck,
            target_front,
            target_back,
            target_deck,
            base_front,
            base_back,
            note,
            utc_now(),
        ),
    )
    return True


def _module_for_deck(conn, target_id: int, group_id: int | None, deck: str):
    """把目标牌组映射为模块；不在组子树内返回 None。"""
    from ..collect.merge import ensure_module

    group = groups.get_group(conn, group_id)
    if group is None:
        return None
    path = (deck or "").replace("::", "/").strip("/")
    root = group["path"]
    if path == root:
        return int(group["id"])
    if not path.startswith(root + "/"):
        return None
    return ensure_module(conn, path[len(root) + 1 :], "anki", group)


def _detach(conn, kp_id: int, target_id: int) -> None:
    conn.execute(
        "UPDATE anki_sync SET detached = 1, last_seen_at = ? "
        "WHERE kp_id = ? AND target_id = ?",
        (utc_now(), kp_id, target_id),
    )


def _clients_for_targets(guard: bool, exclude: int | None = None) -> dict[int, AnkiClient]:
    with closing(connect()) as conn:
        rows = targets.list_targets(conn, enabled_only=True)
    return {
        int(row["id"]): targets.client_for(row, guard)
        for row in rows
        if int(row["id"]) != exclude
    }


def _first_model_client(
    clients: dict[int, AnkiClient], model_name: str
) -> AnkiClient | None:
    """按目标创建顺序找第一个已存在该模型的客户端，用于复制 note 样式。"""
    for target_id in sorted(clients):
        try:
            if model_name in (clients[target_id].call("modelNames", timeout=15) or []):
                return clients[target_id]
        except AnkiError:
            continue
    return None


def _repair_target_templates(
    client: AnkiClient, model_name: str, source_templates: dict
) -> None:
    """修复目标模型里重复的卡片模板，并移除源中不存在的模板。

    部分容器（anki-connect-server 0.5）的 updateModelTemplates 会把同一份内容
    写到所有模板，导致多个模板正面完全相同；此后任何保存都会被 Anki 拒绝。
    这里先把重复模板改成互不相同的临时正面，再删除源中不存在的模板。
    """
    current = {
        name: dict(body)
        for name, body in (client.call("modelTemplates", modelName=model_name) or {}).items()
    }
    groups: dict[str, list[str]] = {}
    for name, body in current.items():
        groups.setdefault((body.get("Front") or "").strip(), []).append(name)
    duplicated = {
        name for names in groups.values() if len(names) > 1 for name in names
    }
    for group_index, names in enumerate(groups.values()):
        if len(names) < 2:
            continue
        for name_index, name in enumerate(names):
            client.call(
                "modelTemplateAdd",
                timeout=60,
                modelName=model_name,
                template={
                    "Name": name,
                    "Front": f"{{{{Front}}}}<!--cardmine-repair-{group_index}-{name_index}-->",
                    "Back": (current[name].get("Back") or ""),
                },
            )
    source_fronts = {
        (body.get("Front") or "").strip() for body in source_templates.values()
    }
    source_names = list(source_templates)
    missing = [name for name in source_names if name not in current]
    extras = [name for name in current if name not in source_templates]
    # 模板名不一致时优先重命名，避免把目标最后一个模板删掉（Anki 不允许）
    while extras and missing and len(current) <= len(source_names):
        old_name = extras.pop(0)
        new_name = missing.pop(0)
        client.call(
            "modelTemplateRename",
            timeout=60,
            modelName=model_name,
            oldTemplateName=old_name,
            newTemplateName=new_name,
        )
        current[new_name] = current.pop(old_name)
    for name in extras:
        if len(current) <= 1:
            break
        body = current[name]
        # 只清理会与现有模板或源模板正面冲突的多余模板，保留用户自定义的独立模板
        if name in duplicated or (body.get("Front") or "").strip() in source_fronts:
            client.call(
                "modelTemplateRemove",
                timeout=60,
                modelName=model_name,
                templateName=name,
            )
            current.pop(name, None)


def _apply_model_templates(
    client: AnkiClient, model_name: str, templates: dict
) -> None:
    """按模板名逐个更新，避开部分容器忽略模板名、批量覆盖所有模板的问题。"""
    for name, body in templates.items():
        try:
            client.call(
                "modelTemplateAdd",
                timeout=60,
                modelName=model_name,
                template={
                    "Name": name,
                    "Front": body.get("Front", ""),
                    "Back": body.get("Back", ""),
                },
            )
        except AnkiError as exc:
            message = str(exc).lower()
            if not any(
                word in message
                for word in ("unsupported", "unknown action", "was not found")
            ):
                raise
            client.call(
                "updateModelTemplates",
                model={"name": model_name, "templates": templates},
            )
            return


def sync_model_style(
    target_id: int,
    model_name: str,
    source_target_id: int | None = None,
    guard: bool = True,
    sync_enabled: bool = False,
) -> dict:
    """把某个目标的 note 类型 CSS 与卡片模板复制到目标。"""
    with closing(connect()) as conn:
        target = targets.get_target(conn, target_id)
        if target is None:
            raise LookupError("同步目标不存在")
        rows = targets.list_targets(conn)

    candidates = [row for row in rows if int(row["id"]) != target_id]
    if source_target_id is not None:
        candidates = [
            row for row in candidates if int(row["id"]) == source_target_id
        ]
        if not candidates:
            raise ValueError("源目标不存在或与目标相同")

    source = None
    source_row = None
    for row in candidates:
        candidate = targets.client_for(row, guard)
        try:
            if model_name in (candidate.call("modelNames", timeout=15) or []):
                source, source_row = candidate, row
                break
        except AnkiError:
            continue
    if source is None or source_row is None:
        raise AnkiError(f"没有其他目标包含模型 {model_name}，无法复制样式")

    styling = source.call("modelStyling", modelName=model_name) or {}
    templates = source.call("modelTemplates", modelName=model_name) or {}
    if not styling.get("css") and not templates:
        raise AnkiError(f"源目标模型 {model_name} 没有可复制的样式")
    fronts = [(body.get("Front") or "").strip() for body in templates.values()]
    if len(set(fronts)) != len(fronts):
        raise AnkiError(
            f"源目标模型 {model_name} 的卡片模板正面重复，请先在源目标修复"
        )

    client = targets.client_for(target, guard)
    client.ensure_model(model_name, source=source)
    if templates:
        _repair_target_templates(client, model_name, templates)
        current_templates = (
            client.call("modelTemplates", modelName=model_name) or {}
        )
        pending = {
            name: body
            for name, body in templates.items()
            if name in current_templates
            and (
                current_templates[name].get("Front") != body.get("Front")
                or current_templates[name].get("Back") != body.get("Back")
            )
        }
        if pending:
            _apply_model_templates(client, model_name, pending)
        for name, body in templates.items():
            if name not in current_templates:
                _apply_model_templates(client, model_name, {name: body})
    client.call(
        "updateModelStyling",
        model={"name": model_name, "css": styling.get("css") or ""},
    )

    # 读回校验，避免容器不支持写入动作却返回成功
    applied_css = (client.call("modelStyling", modelName=model_name) or {}).get("css")
    applied_templates = client.call("modelTemplates", modelName=model_name) or {}
    if styling.get("css") and (applied_css or "").strip() != styling["css"].strip():
        raise AnkiError(f"目标未接受模型 {model_name} 的 CSS 更新")
    for name, body in templates.items():
        current = applied_templates.get(name)
        if current is None:
            raise AnkiError(f"目标缺少卡片模板 {name}")
        if (current.get("Front") or "").strip() != (body.get("Front") or "").strip() or (
            current.get("Back") or ""
        ).strip() != (body.get("Back") or "").strip():
            raise AnkiError(f"目标未接受卡片模板 {name} 的更新")

    sync_state = "skipped"
    sync_error = None
    media_refs = _template_media_refs(styling.get("css") or "", templates)
    media_copied, media_errors = _copy_template_media(source, client, media_refs)
    if sync_enabled:
        try:
            client.sync_web()
            sync_state = "ok"
        except AnkiError as exc:
            sync_state = "failed"
            sync_error = str(exc)
    return {
        "target_id": target_id,
        "source_target_id": int(source_row["id"]),
        "source_name": source_row["name"],
        "css": bool(styling.get("css")),
        "templates": len(templates),
        "media_copied": media_copied,
        "media_errors": media_errors,
        "sync": sync_state,
        "sync_error": sync_error,
    }


def _propagate_delete(
    conn,
    kp_id: int,
    source_target_id: int,
    clients: dict[int, AnkiClient],
    errors: list[str],
) -> int:
    """确认删除后归档卡片，并从其他目标删除同一张卡。"""
    now = utc_now()
    conn.execute(
        "UPDATE knowledge_point SET status = 'archived', updated_at = ? WHERE id = ?",
        (now, kp_id),
    )
    rows = conn.execute(
        "SELECT target_id, anki_note_id FROM anki_sync "
        "WHERE kp_id = ? AND anki_note_id IS NOT NULL",
        (kp_id,),
    ).fetchall()
    deleted = 0
    for row in rows:
        target_id = int(row["target_id"])
        note_id = int(row["anki_note_id"])
        if target_id == source_target_id:
            continue
        client = clients.get(target_id)
        if client is None:
            continue
        try:
            client.call("deleteNotes", timeout=15, notes=[note_id])
            deleted += 1
        except AnkiError as exc:
            if "not found" not in str(exc).lower():
                errors.append(f"卡片 {kp_id}: 目标 {target_id} 删除失败：{exc}")
    conn.execute("DELETE FROM anki_sync WHERE kp_id = ?", (kp_id,))
    conn.execute(
        "DELETE FROM sync_conflict WHERE kp_id = ? AND status = 'pending'",
        (kp_id,),
    )
    return deleted


def _delete_archived_notes(
    conn, client: AnkiClient, target_id: int, progress: Progress | None
) -> tuple[int, list[str]]:
    rows = conn.execute(
        """
        SELECT s.kp_id, s.anki_note_id
        FROM anki_sync s JOIN knowledge_point kp ON kp.id = s.kp_id
        WHERE s.target_id = ? AND s.anki_note_id IS NOT NULL
          AND kp.status = 'archived'
        ORDER BY s.kp_id
        """,
        (target_id,),
    ).fetchall()
    deleted = 0
    errors: list[str] = []
    for index, row in enumerate(rows, start=1):
        note_id = int(row["anki_note_id"])
        try:
            client.call("deleteNotes", timeout=15, notes=[note_id])
        except AnkiError as exc:
            if "not found" not in str(exc).lower():
                errors.append(f"归档卡片 {row['kp_id']}: {exc}")
                conn.execute(
                    "UPDATE anki_sync SET last_error = ? "
                    "WHERE kp_id = ? AND target_id = ?",
                    (str(exc), row["kp_id"], target_id),
                )
                _notify(progress, "清理归档", index, len(rows))
                continue
        conn.execute(
            "DELETE FROM anki_sync WHERE kp_id = ? AND target_id = ?",
            (row["kp_id"], target_id),
        )
        conn.execute(
            "DELETE FROM sync_conflict WHERE kp_id = ? AND status = 'pending'",
            (row["kp_id"],),
        )
        deleted += 1
        _notify(progress, "清理归档", index, len(rows))
    return deleted, errors


def _clear_pending_flags(
    conn, client: AnkiClient, target_id: int, progress: Progress | None
) -> tuple[int, list[str]]:
    rows = conn.execute(
        """
        SELECT s.kp_id, s.anki_note_id, s.flag_clear_pending
        FROM anki_sync s JOIN knowledge_point kp ON kp.id = s.kp_id
        WHERE s.target_id = ? AND s.flag_clear_pending > 0
          AND s.anki_note_id IS NOT NULL AND kp.status != 'archived'
        ORDER BY s.kp_id
        """,
        (target_id,),
    ).fetchall()
    if not rows:
        return 0, []

    note_ids = {int(row["anki_note_id"]) for row in rows}
    infos = _fetch_notes(client, note_ids, progress, phase="读取旗帜")
    note_flags = _fetch_note_flags(client, note_ids, progress)

    cleared = 0
    errors: list[str] = []
    for index, row in enumerate(rows, start=1):
        kp_id = int(row["kp_id"])
        note_id = int(row["anki_note_id"])
        pending = int(row["flag_clear_pending"])
        info = infos.get(note_id)
        if info is None:
            conn.execute(
                "UPDATE anki_sync SET anki_flag = 0, flag_clear_pending = 0 "
                "WHERE kp_id = ? AND target_id = ?",
                (kp_id, target_id),
            )
            _notify(progress, "清除旗帜", index, len(rows))
            continue
        current, cards = note_flags.get(note_id, (0, []))
        if current == 0:
            conn.execute(
                "UPDATE anki_sync SET anki_flag = 0, flag_clear_pending = 0 "
                "WHERE kp_id = ? AND target_id = ?",
                (kp_id, target_id),
            )
            _notify(progress, "清除旗帜", index, len(rows))
            continue
        if current != pending:
            conn.execute(
                "UPDATE anki_sync SET anki_flag = ?, flag_clear_pending = 0 "
                "WHERE kp_id = ? AND target_id = ?",
                (current, kp_id, target_id),
            )
            errors.append(f"卡片 {kp_id}: 目标旗帜已变为 {current}，未清除")
            _notify(progress, "清除旗帜", index, len(rows))
            continue
        try:
            for card_id in cards:
                client.call(
                    "setSpecificValueOfCard",
                    card=card_id,
                    keys=["flags"],
                    newValues=["0"],
                )
        except AnkiError as exc:
            errors.append(f"卡片 {kp_id}: 清除旗帜失败：{exc}")
            _notify(progress, "清除旗帜", index, len(rows))
            continue
        conn.execute(
            "UPDATE anki_sync SET anki_flag = 0, flag_clear_pending = 0 "
            "WHERE kp_id = ? AND target_id = ?",
            (kp_id, target_id),
        )
        cleared += 1
        _notify(progress, "清除旗帜", index, len(rows))
    return cleared, errors


def _merge_existing(
    conn,
    client: AnkiClient,
    model_name: str,
    target_id: int,
    row,
    info: dict,
    deck: str,
    decks: set[str],
    mode: str,
    media_conflicts: set[str],
) -> dict:
    result = {
        "changed": 0,
        "added": 0,
        "updated": 0,
        "pulled": 0,
        "conflicts": 0,
        "skipped": False,
        "detached": 0,
    }
    kp_id = int(row["id"])
    note_id = int(row["anki_note_id"])
    group_id = row["group_id"]
    system_deck = _deck_name(row["module_path"])
    front = row["front_md"] or ""
    back = row["back_md"] or ""
    target_front = _target_field_markdown(info, "Front")
    target_back = _target_field_markdown(info, "Back")

    system_front_hash = field_hash(front)
    system_back_hash = field_hash(back)
    target_front_hash = field_hash(target_front)
    target_back_hash = field_hash(target_back)

    base_front = row["pushed_front_hash"]
    base_back = row["pushed_back_hash"]
    if (base_front is None or base_back is None) and row["pushed_hash"]:
        if row["pushed_hash"] == _system_content_hash(front, back, system_deck):
            base_front = base_front or system_front_hash
            base_back = base_back or system_back_hash

    front_action = field_action(system_front_hash, target_front_hash, base_front)
    back_action = field_action(system_back_hash, target_back_hash, base_back)
    force = mode == "push" and bool(row["force_push"])
    if force:
        front_action = "same" if system_front_hash == target_front_hash else "push"
        back_action = "same" if system_back_hash == target_back_hash else "push"
    if mode != "push":
        if front_action == "push":
            front_action = "none"
        if back_action == "push":
            back_action = "none"

    # 组内移动：目标侧牌组变化写回模块，再分发到其他目标
    if (
        mode == "push"
        and row["pushed_deck"]
        and deck != row["pushed_deck"]
        and not force
    ):
        moved_module = _module_for_deck(conn, target_id, group_id, deck)
        if moved_module is not None and moved_module != row["module_id"]:
            conn.execute(
                "UPDATE knowledge_point SET module_id = ?, updated_at = ? WHERE id = ?",
                (moved_module, utc_now(), kp_id),
            )
            system_deck = deck
            result["changed"] = 1

    back_note = ""
    if back_action == "pull" and not target_back:
        back_action = "conflict"
        back_note = "目标背面为空，未自动采纳"

    if back_action == "pull":
        media_errors = _pull_media(conn, client, local_media_refs(target_back))
        if media_errors:
            back_action = "conflict"
            back_note = "；".join(media_errors)

    if front_action == "pull":
        norm = normalize_front(target_front)
        if not norm:
            front_action = "conflict"
            back_note = back_note or "目标正面为空，未自动采纳"
        else:
            collision = _front_collision(conn, kp_id, group_id, norm)
            if collision is not None:
                front_action = "conflict"
                back_note = f"目标正面与卡片 #{collision['id']} 重复"

    conflict = front_action == "conflict" or back_action == "conflict"
    if conflict:
        _record_conflict(
            conn,
            target_id,
            kp_id,
            note_id,
            "duplicate" if "重复" in back_note else "edit",
            back_note or "目标与系统都修改了同一张卡片",
            front,
            back,
            system_deck,
            target_front,
            target_back,
            deck,
            base_front,
            base_back,
        )
        result["conflicts"] = 1
        result["changed"] = 1
        return result

    fields: dict[str, str] = {}
    if front_action == "push":
        fields["Front"] = front
    if back_action == "push":
        fields["Back"] = back

    if fields:
        bad_media = _markdown_media(
            fields.get("Front", ""), fields.get("Back", "")
        ) & media_conflicts
        if bad_media:
            _record_conflict(
                conn,
                target_id,
                kp_id,
                note_id,
                "media",
                f"媒体 {', '.join(sorted(bad_media))} 在目标中内容不同，未覆盖",
                front,
                back,
                system_deck,
                target_front,
                target_back,
                deck,
                base_front,
                base_back,
            )
            result["conflicts"] = 1
            result["changed"] = 1
            return result

    applied_front = False
    applied_back = False
    if front_action == "pull":
        conn.execute(
            "UPDATE knowledge_point SET front_md = ?, front_norm = ?, updated_at = ? "
            "WHERE id = ?",
            (target_front, normalize_front(target_front), utc_now(), kp_id),
        )
        front = target_front
        system_front_hash = target_front_hash
        applied_front = True
    if back_action == "pull":
        conn.execute(
            "UPDATE knowledge_point SET back_md = ?, updated_at = ? WHERE id = ?",
            (target_back, utc_now(), kp_id),
        )
        back = target_back
        system_back_hash = target_back_hash
        applied_back = True
    if applied_front or applied_back:
        result["pulled"] = 1
        result["changed"] = 1

    reused_note = False
    if fields:
        try:
            client.call(
                "updateNoteFields",
                note={"id": note_id, "fields": fields},
            )
        except AnkiError as exc:
            if "not found" not in str(exc).lower():
                raise
            _ensure_deck(client, decks, system_deck)
            new_id = _add_note(client, model_name, system_deck, front, back)
            if new_id is None:
                new_id = _find_note_by_front(client, system_deck, front)
                if new_id is None:
                    raise exc
                reused_note = True
            note_id = int(new_id)
        result["updated"] = 1
        result["changed"] = 1

    if mode == "push" and row["pushed_deck"] and row["pushed_deck"] != system_deck:
        _ensure_deck(client, decks, system_deck)
        _move_to_deck(client, note_id, system_deck)
        result["changed"] = 1

    if force:
        front_action = "same"
        back_action = "same"

    if front_action in ("push", "pull", "same"):
        base_front = system_front_hash
    if back_action in ("push", "pull", "same"):
        base_back = system_back_hash
    if reused_note:
        base_back = None

    combined = _UNSET
    pushed_at = _UNSET
    pending_left = conn.execute(
        "SELECT COUNT(*) AS value FROM sync_conflict "
        "WHERE kp_id = ? AND status = 'pending'",
        (kp_id,),
    ).fetchone()["value"]
    if mode == "push" and pending_left == 0 and not reused_note:
        combined = _system_content_hash(front, back, system_deck)
        pushed_at = utc_now()
    _upsert_sync(
        conn,
        kp_id,
        target_id,
        note_id,
        row["pushed_deck"] if mode != "push" else system_deck,
        base_front,
        base_back,
        combined=combined,
        pushed_at=pushed_at,
    )
    if mode == "push":
        conn.execute(
            "UPDATE anki_sync SET force_push = 0 WHERE kp_id = ? AND target_id = ?",
            (kp_id, target_id),
        )
    return result


def _push_new_note(
    conn,
    client: AnkiClient,
    model_name: str,
    target_id: int,
    row,
    deck: str,
    decks: set[str],
) -> dict:
    result = {
        "changed": 0,
        "added": 0,
        "updated": 0,
        "pulled": 0,
        "conflicts": 0,
        "skipped": False,
        "detached": 0,
    }
    kp_id = int(row["id"])
    front = row["front_md"] or ""
    back = row["back_md"] or ""
    _ensure_deck(client, decks, deck)
    new_id = _add_note(client, model_name, deck, front, back)
    reused = False
    if new_id is None:
        new_id = _find_note_by_front(client, deck, front)
        if new_id is None:
            raise AnkiError("addNote 返回空且未找到同名笔记")
        reused = True
    combined = None if reused else _system_content_hash(front, back, deck)
    _upsert_sync(
        conn,
        kp_id,
        target_id,
        int(new_id),
        deck,
        field_hash(front),
        None if reused else field_hash(back),
        combined=combined,
        pushed_at=None if reused else utc_now(),
    )
    conn.execute(
        "UPDATE anki_sync SET force_push = 0 WHERE kp_id = ? AND target_id = ?",
        (kp_id, target_id),
    )
    result["changed"] = 1
    if reused:
        result["updated"] = 1
    else:
        result["added"] = 1
    return result


def _handle_missing_note(
    conn,
    row,
    target_id: int,
    clients: dict[int, AnkiClient],
    errors: list[str],
) -> dict:
    result = {
        "changed": 0,
        "added": 0,
        "updated": 0,
        "pulled": 0,
        "conflicts": 0,
        "skipped": True,
        "detached": 0,
    }
    kp_id = int(row["id"])
    if row["detached"]:
        return result

    front = row["front_md"] or ""
    back = row["back_md"] or ""
    deck = _deck_name(row["module_path"])
    base_front = row["pushed_front_hash"]
    base_back = row["pushed_back_hash"]
    edited = (
        (base_front is not None and field_hash(front) != base_front)
        or (base_back is not None and field_hash(back) != base_back)
    )

    if edited:
        _record_conflict(
            conn,
            target_id,
            kp_id,
            row["anki_note_id"],
            "delete_edit",
            "目标删除了这张卡，但系统侧有未同步的修改",
            front,
            back,
            deck,
            None,
            None,
            None,
            base_front,
            base_back,
        )
        result["conflicts"] = 1
        result["changed"] = 1
        result["skipped"] = False
        return result

    _propagate_delete(conn, kp_id, target_id, clients, errors)
    result["changed"] = 1
    result["skipped"] = False
    return result


def _import_note(
    conn,
    client: AnkiClient,
    target_id: int,
    info: dict,
    deck: str,
    group,
) -> dict:
    """目标侧新增 note：按组正面精确去重后建卡或绑定。"""
    result = {"created": 0, "bound": 0, "conflicts": 0, "skipped": 0}
    front = _target_field_markdown(info, "Front")
    back = _target_field_markdown(info, "Back")
    norm = normalize_front(front)
    if not norm:
        result["skipped"] = 1
        return result
    note_id = int(info["noteId"])
    deck = deck or group["path"].replace("/", "::")
    module_id = _module_for_deck(conn, target_id, int(group["id"]), deck)
    existing = conn.execute(
        "SELECT id, front_md, back_md, status FROM knowledge_point "
        "WHERE front_norm = ? AND group_id = ?",
        (norm, int(group["id"])),
    ).fetchone()

    if existing is None:
        now = utc_now()
        cursor = conn.execute(
            "INSERT INTO knowledge_point"
            "(front_md, back_md, front_norm, module_id, group_id, status, origin, "
            " created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, 'ready', 'anki', ?, ?)",
            (front, back, norm, module_id, int(group["id"]), now, now),
        )
        kp_id = int(cursor.lastrowid)
        _upsert_sync(
            conn,
            kp_id,
            target_id,
            note_id,
            deck,
            field_hash(front),
            field_hash(back),
            combined=_system_content_hash(front, back, deck),
            pushed_at=now,
        )
        result["created"] = 1
        return result

    kp_id = int(existing["id"])
    if existing["status"] == "archived":
        result["skipped"] = 1
        return result
    binding = conn.execute(
        "SELECT anki_note_id FROM anki_sync WHERE kp_id = ? AND target_id = ?",
        (kp_id, target_id),
    ).fetchone()
    if binding is not None and binding["anki_note_id"] is not None:
        if int(binding["anki_note_id"]) == note_id:
            _set_last_seen(conn, kp_id, target_id)
            result["skipped"] = 1
            return result
        _record_conflict(
            conn,
            target_id,
            kp_id,
            note_id,
            "duplicate",
            f"同组已有相同正面并绑定了其他目标笔记 #{binding['anki_note_id']}",
            existing["front_md"],
            existing["back_md"],
            _system_deck(conn, kp_id),
            front,
            back,
            deck,
            None,
            None,
        )
        result["conflicts"] = 1
        return result

    if field_hash(existing["back_md"] or "") == field_hash(back):
        status = "ready" if existing["status"] == "draft" else existing["status"]
        conn.execute(
            "UPDATE knowledge_point SET status = ?, module_id = COALESCE(?, module_id), "
            "updated_at = ? WHERE id = ?",
            (status, module_id, utc_now(), kp_id),
        )
        system_deck = _system_deck(conn, kp_id)
        _upsert_sync(
            conn,
            kp_id,
            target_id,
            note_id,
            deck,
            field_hash(front),
            field_hash(back),
            combined=_system_content_hash(front, back, system_deck),
            pushed_at=utc_now(),
        )
        result["bound"] = 1
        return result

    _record_conflict(
        conn,
        target_id,
        kp_id,
        note_id,
        "duplicate",
        "同组正面相同但背面不同",
        existing["front_md"],
        existing["back_md"],
        _system_deck(conn, kp_id),
        front,
        back,
        deck,
        None,
        None,
    )
    result["conflicts"] = 1
    return result


def _discover_notes(
    client: AnkiClient, scopes: list[str], progress: Progress | None
) -> dict[int, str]:
    if not scopes:
        return {}
    decks = client.call("deckNames") or []
    matched = [deck for deck in decks if targets.deck_in_scope(deck, scopes)]
    found: dict[int, str] = {}
    for index, deck in enumerate(matched, start=1):
        try:
            notes = client.call(
                "findNotes", query=f'deck:"{_search_escape(deck)}"'
            ) or []
            for note_id in notes:
                found[int(note_id)] = deck
        except AnkiError:
            continue
        _notify(progress, "扫描目标牌组", index, len(matched))
    return found


def _match_deck_group(conn, target_id: int, note_decks: dict[int, str], note_id: int):
    deck = note_decks.get(note_id)
    if deck is None:
        return None
    return targets.group_for_deck(conn, target_id, deck)


def _unique_deck(note_decks: dict[int, str], note_id: int, fallback: str | None) -> str:
    deck = note_decks.get(note_id)
    if deck:
        return deck
    return fallback or "Default"


HIGH_FREQ_TOP50 = "cardmine::高频::TOP50"
HIGH_FREQ_TOP100 = "cardmine::高频::TOP100"
HIGH_FREQ_TAGS = (HIGH_FREQ_TOP50, HIGH_FREQ_TOP100)


def _high_freq_desired(conn, target_id: int) -> dict[int, set[str]]:
    """按组内 ready 卡的出现次数取 Top50/Top100，返回 note_id 到标签集合。"""
    rows = conn.execute(
        """
        SELECT kp.id, kp.group_id, kp.updated_at, s.anki_note_id,
               COUNT(me.id) AS mentions
        FROM knowledge_point kp
        JOIN anki_sync s
          ON s.kp_id = kp.id AND s.target_id = ?
         AND s.anki_note_id IS NOT NULL AND s.detached = 0
        JOIN group_target gt
          ON gt.group_id = kp.group_id AND gt.target_id = ?
        LEFT JOIN mention me ON me.kp_id = kp.id
        WHERE kp.status = 'ready'
        GROUP BY kp.id
        """,
        (target_id, target_id),
    ).fetchall()
    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(int(row["group_id"]), []).append(row)

    desired: dict[int, set[str]] = {}
    for group_rows in grouped.values():
        group_rows.sort(
            key=lambda row: (
                int(row["mentions"] or 0),
                row["updated_at"] or "",
                int(row["id"]),
            ),
            reverse=True,
        )
        for rank, row in enumerate(group_rows, start=1):
            if rank > 100:
                break
            tags = {HIGH_FREQ_TOP100}
            if rank <= 50:
                tags.add(HIGH_FREQ_TOP50)
            desired[int(row["anki_note_id"])] = tags
    return desired


def _sync_high_freq_tags(
    conn,
    client: AnkiClient,
    target_id: int,
    progress: Progress | None = None,
) -> dict:
    """把组内高频排名同步为 Anki 标签，只增删 cardmine 自己的标签。"""
    result = {"updated": 0, "changed": 0, "errors": []}
    changed_notes: set[int] = set()
    desired = _high_freq_desired(conn, target_id)
    mapped = {
        int(row["anki_note_id"])
        for row in conn.execute(
            "SELECT anki_note_id FROM anki_sync "
            "WHERE target_id = ? AND anki_note_id IS NOT NULL",
            (target_id,),
        )
    }
    if not mapped and not desired:
        return result

    _notify(progress, "刷新高频标签", 0, 1)
    existing: dict[str, set[int]] = {}
    try:
        for tag in HIGH_FREQ_TAGS:
            ids = client.call("findNotes", query=f"tag:{tag}", timeout=30) or []
            existing[tag] = {int(note_id) for note_id in ids} & mapped
    except AnkiError as exc:
        result["errors"].append(f"读取高频标签失败: {exc}")
        _notify(progress, "刷新高频标签", 1, 1)
        return result

    for tag in HIGH_FREQ_TAGS:
        wanted = {
            note_id
            for note_id, tags in desired.items()
            if tag in tags
        }
        have = existing.get(tag, set())
        add = sorted(wanted - have)
        remove = sorted(have - wanted)
        if add:
            try:
                client.call("addTags", notes=add, tags=tag, timeout=60)
                changed_notes.update(add)
            except AnkiError as exc:
                result["errors"].append(f"写入标签 {tag} 失败: {exc}")
        if remove:
            try:
                client.call("removeTags", notes=remove, tags=tag, timeout=60)
                changed_notes.update(remove)
            except AnkiError as exc:
                result["errors"].append(f"移除标签 {tag} 失败: {exc}")

    result["updated"] = len(changed_notes)
    result["changed"] = 1 if changed_notes else 0
    _notify(progress, "刷新高频标签", 1, 1)
    return result


def sync_target(
    target_id: int,
    model_name: str,
    sync_enabled: bool,
    guard: bool,
    progress: Progress | None = None,
    push: bool = True,
    pull: bool = True,
) -> dict:
    with closing(connect()) as conn:
        target = targets.get_target(conn, target_id)
        if target is None:
            raise LookupError("同步目标不存在")
        scopes = targets.scope_paths(conn, target_id)

    client = targets.client_for(target, guard)
    clients = _clients_for_targets(guard, exclude=target_id)
    _notify(progress, "校验目标", 0, 1)
    client.ensure_model(
        model_name, source=_first_model_client(clients, model_name)
    )
    _notify(progress, "校验目标", 1, 1)

    result = {
        "target_id": target_id,
        "target": target["name"],
        "url": target["base_url"],
        "ready": 0,
        "archived_deleted": 0,
        "changed": 0,
        "added": 0,
        "updated": 0,
        "pulled": 0,
        "conflicts": 0,
        "detached": 0,
        "imported": 0,
        "skipped_conflicts": 0,
        "flags_cleared": 0,
        "tags_updated": 0,
        "failed": 0,
        "media_uploaded": 0,
        "backup": None,
        "sync": "skipped",
        "errors": [],
    }
    errors: list[str] = result["errors"]

    with closing(connect()) as conn, conn:
        archived_deleted, archive_errors = _delete_archived_notes(
            conn, client, target_id, progress
        )
        result["archived_deleted"] = archived_deleted
        errors.extend(archive_errors)

        rows = _load_rows(conn, target_id, ("ready",))
        result["ready"] = len(rows)
        conflicted = _pending_conflict_cards(conn)

        linked_ids = {
            int(row["anki_note_id"])
            for row in rows
            if row["anki_note_id"] is not None
        }
        infos = _fetch_notes(client, linked_ids, progress)
        note_decks = _note_decks(client, infos)

        if pull:
            discovered = _discover_notes(client, scopes, progress)
            new_ids = set(discovered) - linked_ids
            if new_ids:
                new_infos = _fetch_notes(
                    client, new_ids, progress, phase="读取新增笔记"
                )
                new_decks = _note_decks(client, new_infos)
                for note_id in sorted(new_infos):
                    info = new_infos[note_id]
                    if info.get("modelName") != model_name:
                        continue
                    deck = _unique_deck(
                        new_decks, note_id, discovered.get(note_id)
                    )
                    group = targets.group_for_deck(conn, target_id, deck)
                    if group is None:
                        continue
                    try:
                        imported = _import_note(
                            conn, client, target_id, info, deck, group
                        )
                        result["imported"] += imported["created"] + imported["bound"]
                        result["conflicts"] += imported["conflicts"]
                        result["changed"] += imported["created"] + imported["bound"]
                        conn.commit()
                    except AnkiError as exc:
                        result["failed"] += 1
                        errors.append(f"目标新增 note {note_id}: {exc}")
                        conn.rollback()
                # 导入可能给已有卡绑定 note，刷新行避免随后重复推送新 note
                infos.update(new_infos)
                note_decks.update(new_decks)
                rows = _load_rows(conn, target_id, ("ready",))
                linked_ids = {
                    int(row["anki_note_id"])
                    for row in rows
                    if row["anki_note_id"] is not None
                }
                conflicted = _pending_conflict_cards(conn)

        media_refs: set[str] = set()
        if push:
            for row in rows:
                if int(row["id"]) in conflicted:
                    continue
                media_refs |= {
                    normalize_media_ref(name)
                    for name in _markdown_media(row["front_md"], row["back_md"])
                    if _is_local_media(name)
                }
        media_conflicts: set[str] = set()
        if push and media_refs:
            uploaded, media_conflicts, media_errors = _push_media(
                client, media_refs
            )
            result["media_uploaded"] = uploaded
            errors.extend(media_errors)

        flags = {}
        if pull and infos:
            try:
                flags = _fetch_note_flags(client, set(infos), progress)
            except AnkiError as exc:
                errors.append(str(exc))

        decks: set[str] = set()
        if push:
            decks = set(client.call("deckNames") or [])

        group_paths = {
            int(row["id"]): row["path"]
            for row in conn.execute(
                "SELECT m.id, m.path FROM group_target gt "
                "JOIN module m ON m.id = gt.group_id WHERE gt.target_id = ?",
                (target_id,),
            )
        }

        for index, row in enumerate(rows, start=1):
            kp_id = int(row["id"])
            note_id = row["anki_note_id"]
            info = infos.get(int(note_id)) if note_id is not None else None
            if kp_id in conflicted:
                result["skipped_conflicts"] += 1
                _notify(progress, "同步卡片", index, len(rows))
                continue

            deck = (
                _unique_deck(note_decks, int(note_id), row["pushed_deck"])
                if note_id
                else _deck_name(row["module_path"])
            )
            group_path = group_paths.get(int(row["group_id"])) if row["group_id"] else None
            in_group_scope = bool(group_path) and targets.deck_in_scope(
                deck, [group_path]
            )
            try:
                if note_id is not None and info is None:
                    if push and row["force_push"]:
                        card = _push_new_note(
                            conn,
                            client,
                            model_name,
                            target_id,
                            row,
                            _deck_name(row["module_path"]),
                            decks,
                        )
                    else:
                        card = _handle_missing_note(
                            conn, row, target_id, clients, errors
                        )
                elif info is not None:
                    if not in_group_scope:
                        if not row["detached"]:
                            _detach(conn, kp_id, target_id)
                            result["detached"] += 1
                            result["changed"] += 1
                        card = {"changed": 0, "conflicts": 0}
                    else:
                        card = _merge_existing(
                            conn,
                            client,
                            model_name,
                            target_id,
                            row,
                            info,
                            deck,
                            decks,
                            "push" if push else "pull",
                            media_conflicts,
                        )
                        if row["detached"]:
                            conn.execute(
                                "UPDATE anki_sync SET detached = 0 "
                                "WHERE kp_id = ? AND target_id = ?",
                                (kp_id, target_id),
                            )
                        if pull and note_id is not None:
                            _record_flag(
                                conn,
                                kp_id,
                                target_id,
                                flags.get(int(note_id), (0, []))[0],
                            )
                else:
                    card = (
                        _push_new_note(
                            conn, client, model_name, target_id, row, _deck_name(row["module_path"]), decks
                        )
                        if push
                        else {"changed": 0, "conflicts": 0, "skipped": True}
                    )
                conn.commit()
                result["added"] += card.get("added", 0)
                result["updated"] += card.get("updated", 0)
                result["pulled"] += card.get("pulled", 0)
                result["conflicts"] += card.get("conflicts", 0)
                result["changed"] += card.get("changed", 0)
            except AnkiError as exc:
                result["failed"] += 1
                errors.append(f"卡片 {kp_id}: {exc}")
                conn.rollback()
                _default_error(conn, kp_id, target_id, str(exc))
                conn.commit()
            _notify(progress, "同步卡片", index, len(rows))

        if push:
            flags_cleared, flag_errors = _clear_pending_flags(
                conn, client, target_id, progress
            )
            result["flags_cleared"] = flags_cleared
            errors.extend(flag_errors)
            result["changed"] += flags_cleared

        tag_result = _sync_high_freq_tags(conn, client, target_id, progress)
        result["tags_updated"] = tag_result["updated"]
        result["changed"] += tag_result["changed"]
        errors.extend(tag_result["errors"])

        if sync_enabled and result["changed"]:
            _notify(progress, "备份集合", 0, 1)
            if target["base_url"].rstrip("/") == config.ANKI_CONNECT_URL.rstrip("/"):
                result["backup"] = backup_anki()
            _notify(progress, "备份集合", 1, 1)
            result["sync"] = "ok"
            try:
                if result["media_uploaded"]:
                    client.call("syncMedia", timeout=600)
                client.sync_web()
            except AnkiError as exc:
                result["sync"] = f"failed: {exc}"
                errors.append(f"同步失败: {exc}")

        targets.record_target_sync(conn, target_id, None)
        conn.commit()

    return result


def _aggregate(results: list[dict], action: str) -> dict:
    if len(results) == 1:
        return results[0]
    total = {
        "action": action,
        "changed": 0,
        "added": 0,
        "updated": 0,
        "pulled": 0,
        "conflicts": 0,
        "detached": 0,
        "failed": 0,
        "archived_deleted": 0,
        "flags_cleared": 0,
        "tags_updated": 0,
        "media_uploaded": 0,
        "errors": [],
        "targets": results,
    }
    for item in results:
        for key in (
            "changed",
            "added",
            "updated",
            "pulled",
            "conflicts",
            "detached",
            "failed",
            "archived_deleted",
            "flags_cleared",
            "tags_updated",
            "media_uploaded",
        ):
            total[key] += item.get(key, 0)
        name = item.get("target") or item.get("target_id")
        total["errors"].extend(f"[{name}] {error}" for error in item.get("errors", []))
    return total


def _selected_targets(target_ids: list[int] | None) -> list[int]:
    with closing(connect()) as conn:
        rows = targets.list_targets(conn, enabled_only=True)
    ids = [int(row["id"]) for row in rows]
    if target_ids:
        wanted = {int(item) for item in target_ids}
        ids = [target_id for target_id in ids if target_id in wanted]
    return ids


def push_targets(
    model_name: str,
    sync_enabled: bool,
    guard: bool,
    progress: Progress | None = None,
    target_ids: list[int] | None = None,
) -> dict:
    results = []
    for target_id in _selected_targets(target_ids):
        try:
            results.append(
                sync_target(
                    target_id,
                    model_name,
                    sync_enabled,
                    guard,
                    progress,
                    push=True,
                    pull=True,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单目标失败不阻塞其他目标
            with closing(connect()) as conn, conn:
                targets.record_target_sync(conn, target_id, str(exc))
            results.append(
                {
                    "target_id": target_id,
                    "target": str(target_id),
                    "changed": 0,
                    "added": 0,
                    "updated": 0,
                    "pulled": 0,
                    "conflicts": 0,
                    "failed": 1,
                    "errors": [str(exc)],
                }
            )
    return _aggregate(results, "push")


def pull_target(
    target_id: int,
    model_name: str,
    sync_enabled: bool,
    guard: bool,
    progress: Progress | None = None,
) -> dict:
    return sync_target(
        target_id, model_name, sync_enabled, guard, progress, push=False, pull=True
    )


def sync_all_targets(
    model_name: str,
    sync_enabled: bool,
    guard: bool,
    progress: Progress | None = None,
    target_ids: list[int] | None = None,
) -> dict:
    results = []
    for target_id in _selected_targets(target_ids):
        try:
            result = sync_target(
                target_id, model_name, sync_enabled, guard, progress
            )
            result["action"] = "sync"
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            with closing(connect()) as conn, conn:
                targets.record_target_sync(conn, target_id, str(exc))
            results.append(
                {
                    "target_id": target_id,
                    "target": str(target_id),
                    "changed": 0,
                    "added": 0,
                    "updated": 0,
                    "pulled": 0,
                    "conflicts": 0,
                    "failed": 1,
                    "errors": [str(exc)],
                }
            )
    return _aggregate(results, "sync")


def takeover_target(
    target_id: int, model_name: str, guard: bool, progress: Progress | None = None
) -> dict:
    with closing(connect()) as conn:
        target = targets.get_target(conn, target_id)
        if target is None:
            raise LookupError("同步目标不存在")
        scopes = targets.scope_paths(conn, target_id)
    client = targets.client_for(target, guard)
    _notify(progress, "校验目标", 0, 1)
    client.ensure_model(model_name)
    _notify(progress, "校验目标", 1, 1)

    if not scopes:
        return {
            "target_id": target_id,
            "target": target["name"],
            "scanned_decks": 0,
            "scanned_notes": 0,
            "matched": 0,
            "unmatched_local": 0,
            "unmatched_anki": 0,
            "errors": [],
        }

    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT kp.id, kp.front_norm, kp.group_id
            FROM knowledge_point kp
            JOIN group_target gt ON gt.group_id = kp.group_id AND gt.target_id = ?
            LEFT JOIN anki_sync s ON s.kp_id = kp.id AND s.target_id = ?
            WHERE kp.status != 'archived'
              AND (s.anki_note_id IS NULL)
            """,
            (target_id, target_id),
        ).fetchall()
        pending = {(row["group_id"], row["front_norm"]): row["id"] for row in rows}
        by_norm: dict[str, list[tuple[int | None, int]]] = {}
        for row in rows:
            by_norm.setdefault(row["front_norm"], []).append(
                (row["group_id"], row["id"])
            )

    discovered = _discover_notes(client, scopes, progress)
    notes = _fetch_notes(client, set(discovered), progress, phase="匹配笔记")
    decks = _note_decks(client, notes)
    matches: list[tuple[int, int, str, str, str]] = []
    other_notes = 0
    for note_id, info in notes.items():
        if info.get("modelName") != model_name:
            other_notes += 1
            continue
        deck = _unique_deck(decks, note_id, discovered.get(note_id))
        with closing(connect()) as conn:
            group = targets.group_for_deck(conn, target_id, deck)
        if group is None:
            continue
        norm = normalize_front(_target_field_markdown(info, "Front"))
        kp_id = pending.pop((int(group["id"]), norm), None)
        if kp_id is None:
            candidates = [
                item
                for item in by_norm.get(norm, [])
                if pending.get((item[0], norm)) == item[1]
            ]
            if len(candidates) == 1:
                _, kp_id = candidates[0]
                pending.pop((candidates[0][0], norm), None)
        if kp_id is None:
            continue
        front = _target_field_markdown(info, "Front")
        back = _target_field_markdown(info, "Back")
        matches.append((kp_id, note_id, deck, field_hash(front), field_hash(back)))

    with closing(connect()) as conn, conn:
        for kp_id, note_id, deck, front_hash, back_hash in matches:
            _upsert_sync(conn, kp_id, target_id, note_id, deck, front_hash, back_hash)
        conn.commit()

    return {
        "target_id": target_id,
        "target": target["name"],
        "scanned_decks": len(scopes),
        "scanned_notes": len(notes),
        "matched": len(matches),
        "unmatched_local": len(pending),
        "unmatched_anki": max(0, len(notes) - len(matches) - other_notes),
        "errors": [],
    }


def audit_target(
    target_id: int, model_name: str, guard: bool, progress: Progress | None = None
) -> dict:
    with closing(connect()) as conn:
        target = targets.get_target(conn, target_id)
        if target is None:
            raise LookupError("同步目标不存在")
        scopes = targets.scope_paths(conn, target_id)
        decks = set(scopes)
        decks.update(
            row["pushed_deck"]
            for row in conn.execute(
                "SELECT DISTINCT pushed_deck FROM anki_sync "
                "WHERE target_id = ? AND pushed_deck IS NOT NULL AND pushed_deck != ''",
                (target_id,),
            )
        )
        db_notes = {
            int(row["anki_note_id"]): dict(row)
            for row in conn.execute(
                """
                SELECT s.kp_id AS id, s.anki_note_id, kp.front_md, kp.status,
                       m.path AS module_path
                FROM anki_sync s
                JOIN knowledge_point kp ON kp.id = s.kp_id
                LEFT JOIN module m ON m.id = kp.module_id
                WHERE s.target_id = ? AND s.anki_note_id IS NOT NULL
                """,
                (target_id,),
            )
        }
    client = targets.client_for(target, guard)
    client.ensure_model(model_name)

    errors: list[str] = []
    note_decks: dict[int, str] = {}
    ordered_decks = sorted(decks)
    for index, deck in enumerate(ordered_decks, start=1):
        try:
            found = client.call(
                "findNotes", query=f'deck:"{_search_escape(deck)}"'
            )
            for note_id in found or []:
                note_decks[int(note_id)] = deck
        except AnkiError as exc:
            errors.append(f"{deck}: {exc}")
        _notify(progress, "扫描牌组", index, len(ordered_decks))

    infos = _fetch_notes(client, set(note_decks), progress, phase="读取笔记")
    orphans = []
    for note_id in sorted(infos, reverse=True):
        info = infos[note_id]
        if note_id in db_notes or info.get("modelName") != model_name:
            continue
        fronts = info.get("fields") or {}
        orphans.append(
            {
                "note_id": note_id,
                "deck": note_decks.get(note_id, ""),
                "model": info["modelName"],
                "front": _plain_text(
                    anki_to_markdown((fronts.get("Front") or {}).get("value", ""))
                )[:120],
                "back": _plain_text(
                    anki_to_markdown((fronts.get("Back") or {}).get("value", ""))
                )[:120],
            }
        )

    missing = []
    for note_id, row in db_notes.items():
        if note_id in infos:
            continue
        missing.append(
            {
                "kp_id": row["id"],
                "note_id": note_id,
                "front": _plain_text(row["front_md"] or "")[:120],
                "module_path": row["module_path"] or "",
                "status": row["status"],
            }
        )

    return {
        "target_id": target_id,
        "target": target["name"],
        "decks": ordered_decks,
        "model": model_name,
        "scanned_notes": len(infos),
        "system_notes": len(db_notes),
        "orphan_total": len(orphans),
        "merged_total": 0,
        "missing_total": len(missing),
        "orphans": orphans[:AUDIT_LIMIT],
        "missing": missing[:AUDIT_LIMIT],
        "errors": errors,
    }


def _conflict_detail(conn, conflict_id: int) -> dict:
    row = conn.execute(
        """
        SELECT c.*, t.name AS target_name,
               kp.front_md, kp.back_md, kp.status AS card_status,
               m.path AS module_path
        FROM sync_conflict c
        JOIN knowledge_point kp ON kp.id = c.kp_id
        LEFT JOIN anki_target t ON t.id = c.target_id
        LEFT JOIN module m ON m.id = kp.module_id
        WHERE c.id = ?
        """,
        (conflict_id,),
    ).fetchone()
    if row is None:
        raise LookupError("冲突不存在")
    item = dict(row)
    item["front_preview"] = _plain_text(item["front_md"] or "")[:160]
    return item


def _conflict_list_item(row) -> dict:
    item = dict(row)
    item["front_preview"] = _plain_text(item.get("front_md") or "")[:160]
    item.pop("front_md", None)
    return item


def list_conflicts(status: str = "pending", limit: int = 100) -> dict:
    if status not in ("pending", "resolved"):
        raise ValueError("不支持的冲突状态")
    limit = max(1, min(limit, 500))
    with closing(connect()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS value FROM sync_conflict WHERE status = ?",
            (status,),
        ).fetchone()["value"]
        rows = conn.execute(
            """
            SELECT c.id, c.kp_id, c.target_id, c.anki_note_id, c.kind,
                   c.system_front, c.system_back, c.system_deck,
                   c.target_front, c.target_back, c.target_deck,
                   c.note, c.status, c.resolution, c.created_at, c.resolved_at,
                   kp.front_md, kp.status AS card_status, m.path AS module_path,
                   t.name AS target_name
            FROM sync_conflict c
            JOIN knowledge_point kp ON kp.id = c.kp_id
            LEFT JOIN anki_target t ON t.id = c.target_id
            LEFT JOIN module m ON m.id = kp.module_id
            WHERE c.status = ?
            ORDER BY c.id DESC LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    return {
        "total": total,
        "status": status,
        "items": [_conflict_list_item(row) for row in rows],
    }


def _force_push_all(conn, kp_id: int) -> None:
    conn.execute(
        "UPDATE anki_sync SET force_push = 1, detached = 0 WHERE kp_id = ?",
        (kp_id,),
    )


def resolve_conflict(conflict_id: int, choice: str) -> dict:
    """整卡选择：保留系统内容，或采用目标内容并分发到所有目标。"""
    if choice not in ("system", "target"):
        raise ValueError("不支持的解决方式")
    with closing(connect()) as conn, conn:
        conflict = conn.execute(
            "SELECT * FROM sync_conflict WHERE id = ?", (conflict_id,)
        ).fetchone()
        if conflict is None:
            raise LookupError("冲突不存在")
        if conflict["status"] != "pending":
            raise RuntimeError("该冲突已处理")

        card = conn.execute(
            "SELECT id, front_md, back_md, front_norm, group_id, status "
            "FROM knowledge_point WHERE id = ?",
            (conflict["kp_id"],),
        ).fetchone()
        if card is None:
            raise LookupError("卡片不存在")

        if choice == "target":
            front = conflict["target_front"] or ""
            back = conflict["target_back"] or ""
            if not front:
                raise ValueError("目标版本没有可用的正面，无法采用")
            norm = normalize_front(front)
            collision = _front_collision(
                conn, card["id"], card["group_id"], norm
            )
            if collision is not None:
                raise ValueError(f"目标正面与卡片 #{collision['id']} 重复")
            module_id = card["group_id"]
            deck = conflict["target_deck"]
            if deck:
                module_id = _module_for_deck(
                    conn, conflict["target_id"] or 0, card["group_id"], deck
                ) or module_id
            status = "ready" if card["status"] == "draft" else card["status"]
            conn.execute(
                "UPDATE knowledge_point SET front_md = ?, back_md = ?, "
                "front_norm = ?, status = ?, module_id = COALESCE(?, module_id), "
                "updated_at = ? WHERE id = ?",
                (front, back, norm, status, module_id, utc_now(), card["id"]),
            )

        conn.execute(
            "UPDATE sync_conflict SET status = 'resolved', resolution = ?, "
            "resolved_at = ? WHERE id = ?",
            (choice, utc_now(), conflict_id),
        )
        _force_push_all(conn, card["id"])
        return _conflict_detail(conn, conflict_id)


def undo_conflict(conflict_id: int) -> dict:
    """撤销「用目标」，恢复系统快照并重新打开冲突。"""
    with closing(connect()) as conn, conn:
        conflict = conn.execute(
            "SELECT * FROM sync_conflict WHERE id = ?", (conflict_id,)
        ).fetchone()
        if conflict is None:
            raise LookupError("冲突不存在")
        if conflict["status"] != "resolved" or conflict["resolution"] != "target":
            raise RuntimeError("只有「用目标」的解决可以撤销")

        conn.execute(
            "UPDATE knowledge_point SET front_md = ?, back_md = ?, front_norm = ?, "
            "updated_at = ? WHERE id = ?",
            (
                conflict["system_front"],
                conflict["system_back"],
                normalize_front(conflict["system_front"]),
                utc_now(),
                conflict["kp_id"],
            ),
        )
        conn.execute(
            "UPDATE sync_conflict SET status = 'pending', resolution = NULL, "
            "resolved_at = NULL, note = ? WHERE id = ?",
            ("已撤销「用目标」，冲突重新打开。", conflict_id),
        )
        _force_push_all(conn, conflict["kp_id"])
        return _conflict_detail(conn, conflict_id)


def pending_push_count(conn, target_id: int | None = None) -> int:
    clause = "AND gt.target_id = ?" if target_id is not None else ""
    params = (target_id,) if target_id is not None else ()
    rows = conn.execute(
        f"""
        SELECT kp.id, kp.front_md, kp.back_md, m.path AS module_path,
               gt.target_id, s.pushed_hash
        FROM knowledge_point kp
        JOIN group_target gt ON gt.group_id = kp.group_id
        JOIN anki_target t ON t.id = gt.target_id AND t.enabled = 1
        LEFT JOIN module m ON m.id = kp.module_id
        LEFT JOIN anki_sync s
          ON s.kp_id = kp.id AND s.target_id = gt.target_id
        WHERE kp.status = 'ready' {clause}
        """,
        params,
    ).fetchall()
    pending = 0
    for row in rows:
        deck = _deck_name(row["module_path"])
        media = _markdown_media(row["front_md"], row["back_md"])
        signature = _content_hash(row["front_md"], row["back_md"], deck, media)
        if signature != row["pushed_hash"]:
            pending += 1
    return pending


def summary() -> dict:
    """本地同步概况：状态计数、待推送、每目标统计与最近失败。"""
    with closing(connect()) as conn:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM knowledge_point GROUP BY status"
            )
        }
        matched = conn.execute(
            "SELECT COUNT(DISTINCT kp_id) AS value FROM anki_sync "
            "WHERE anki_note_id IS NOT NULL AND detached = 0"
        ).fetchone()["value"]
        last_push = conn.execute(
            "SELECT MAX(pushed_at) AS value FROM anki_sync"
        ).fetchone()["value"]
        failures = [
            {"kp_id": row["kp_id"], "target_id": row["target_id"], "error": row["last_error"]}
            for row in conn.execute(
                "SELECT kp_id, target_id, last_error FROM anki_sync "
                "WHERE last_error IS NOT NULL ORDER BY kp_id LIMIT 10"
            )
        ]
        conflicts = conn.execute(
            "SELECT COUNT(*) AS value FROM sync_conflict WHERE status = 'pending'"
        ).fetchone()["value"]
        target_rows = targets.list_targets(conn)
        target_items = []
        for row in target_rows:
            stats = targets.target_stats(conn, int(row["id"]))
            if row["enabled"]:
                stats["pending"] = pending_push_count(conn, int(row["id"]))
            target_items.append({**dict(row), **stats})
        pending = pending_push_count(conn)

    return {
        "cards": {
            "draft": counts.get("draft", 0),
            "ready": counts.get("ready", 0),
            "archived": counts.get("archived", 0),
            "matched": matched,
            "pending_push": pending,
        },
        "last_push_at": last_push,
        "conflicts": conflicts,
        "failures": failures,
        "targets": target_items,
    }
