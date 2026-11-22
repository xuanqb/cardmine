"""接管匹配、增量推送与同步。"""

import base64
import hashlib
import html
import json
import mimetypes
import re
from contextlib import closing
from typing import Callable

from .. import config
from ..db import connect, utc_now
from ..export import local_media_refs
from ..importer.textfmt import (
    anki_to_markdown,
    normalize_front,
    normalize_media_ref,
)
from .backup import backup_anki
from .client import AnkiClient, AnkiError

Progress = Callable[[str, int, int], None]
AUDIT_LIMIT = 500
MARKDOWN_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
REMOTE_PREFIXES = ("http://", "https://", "//", "/")

FIELD_LABELS = {"front": "正面", "back": "背面"}
FIELD_NAMES = {"front": "Front", "back": "Back"}
_HTML_HINT_RE = re.compile(
    r"<\s*(?:img|br|pre|code|div|span|p|b|i|u|strong|em|ul|ol|li|table|tr|td|a|font)\b",
    re.I,
)
_FIELD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)\s*\)")
_FIELD_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")


def _notify(progress: Progress | None, phase: str, done: int, total: int) -> None:
    if progress:
        progress(phase, done, total)


def _search_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _markdown_media(*texts: str) -> set[str]:
    refs: set[str] = set()
    for text in texts:
        if not text:
            continue
        refs.update(MARKDOWN_IMG_RE.findall(text))
        refs.update(SOUND_RE.findall(text))
    return refs


def _deck_name(module_path: str | None) -> str:
    return (module_path or "Default").replace("/", "::")


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


def _anki_field(info: dict | None, key: str) -> str:
    if not info:
        return ""
    fields = info.get("fields") or {}
    return (fields.get(key) or {}).get("value", "") or ""


def _deck_of(row) -> str:
    return _deck_name(row["module_path"])


def _system_content_hash(front: str, back: str, deck: str) -> str:
    return _content_hash(front, back, deck, _markdown_media(front, back))


def takeover(client: AnkiClient, model_name: str, progress: Progress | None = None) -> dict:
    _notify(progress, "校验 Anki", 0, 1)
    client.validate(model_name)
    _notify(progress, "校验 Anki", 1, 1)

    with closing(connect()) as conn:
        pending = {
            row["front_norm"]: row["id"]
            for row in conn.execute(
                "SELECT id, front_norm FROM knowledge_point WHERE anki_note_id IS NULL"
            )
        }

    if not pending:
        return {
            "scanned_decks": 0,
            "scanned_notes": 0,
            "matched": 0,
            "unmatched_local": 0,
            "unmatched_anki": 0,
            "errors": [],
        }

    decks = client.call("deckNames")
    note_decks: dict[int, str] = {}
    errors: list[str] = []
    for index, deck in enumerate(decks, start=1):
        try:
            found = client.call("findNotes", query=f'deck:"{_search_escape(deck)}"')
            for note_id in found:
                note_decks[int(note_id)] = deck
        except AnkiError as exc:
            errors.append(f"{deck}: {exc}")
        _notify(progress, "扫描牌组", index, len(decks))

    ordered = sorted(note_decks)
    matches: list[tuple[int, int, str]] = []
    scanned = 0
    for offset in range(0, len(ordered), 500):
        chunk = ordered[offset : offset + 500]
        try:
            infos = client.call("notesInfo", notes=chunk)
        except AnkiError as exc:
            errors.append(f"读取笔记失败: {exc}")
            continue
        for info in infos:
            scanned += 1
            fields = info.get("fields") or {}
            front = (fields.get("Front") or {}).get("value", "")
            norm = normalize_front(anki_to_markdown(front))
            kp_id = pending.pop(norm, None)
            if kp_id is not None:
                note_id = int(info["noteId"])
                matches.append((kp_id, note_id, note_decks.get(note_id, "")))
        _notify(progress, "匹配笔记", min(offset + 500, len(ordered)), len(ordered))

    now = utc_now()
    with closing(connect()) as conn, conn:
        for kp_id, note_id, deck in matches:
            conn.execute(
                "UPDATE knowledge_point SET anki_note_id = ?, updated_at = ? WHERE id = ?",
                (note_id, now, kp_id),
            )
            conn.execute(
                "INSERT INTO anki_sync(kp_id, anki_note_id, pushed_deck) VALUES(?, ?, ?) "
                "ON CONFLICT(kp_id) DO UPDATE SET anki_note_id = excluded.anki_note_id, "
                "pushed_deck = excluded.pushed_deck, last_error = NULL",
                (kp_id, note_id, deck),
            )

    return {
        "scanned_decks": len(decks),
        "scanned_notes": scanned,
        "matched": len(matches),
        "unmatched_local": len(pending),
        "unmatched_anki": max(0, scanned - len(matches)),
        "errors": errors,
    }


def _note_preview(fields: dict, key: str) -> str:
    value = (fields.get(key) or {}).get("value", "")
    return _plain_text(anki_to_markdown(value))[:120]


def audit(
    client: AnkiClient,
    model_name: str,
    progress: Progress | None = None,
) -> dict:
    """体检：找出 Anki 有但系统未关联的 note，以及系统有但 Anki 已删除的关联。"""
    _notify(progress, "校验 Anki", 0, 1)
    client.validate(model_name)
    _notify(progress, "校验 Anki", 1, 1)

    with closing(connect()) as conn:
        decks = {
            _deck_name(row["path"])
            for row in conn.execute("SELECT path FROM module")
            if row["path"]
        }
        decks.update(
            row["deck"]
            for row in conn.execute(
                "SELECT DISTINCT pushed_deck AS deck FROM anki_sync "
                "WHERE pushed_deck IS NOT NULL AND pushed_deck != ''"
            )
        )
        db_notes = {
            row["anki_note_id"]: dict(row)
            for row in conn.execute(
                """
                SELECT kp.id, kp.anki_note_id, kp.front_md, kp.status,
                       m.path AS module_path
                FROM knowledge_point kp
                LEFT JOIN module m ON m.id = kp.module_id
                WHERE kp.anki_note_id IS NOT NULL
                """
            )
        }

    errors: list[str] = []
    note_decks: dict[int, str] = {}
    ordered_decks = sorted(decks)
    for index, deck in enumerate(ordered_decks, start=1):
        try:
            found = client.call(
                "findNotes", query=f'deck:"{_search_escape(deck)}"'
            )
            for note_id in found:
                note_decks[int(note_id)] = deck
        except AnkiError as exc:
            errors.append(f"{deck}: {exc}")
        _notify(progress, "扫描牌组", index, len(ordered_decks))

    ordered = sorted(note_decks)
    infos_by_id: dict[int, dict] = {}
    for offset in range(0, len(ordered), 500):
        chunk = ordered[offset : offset + 500]
        try:
            infos = client.call("notesInfo", notes=chunk)
        except AnkiError as exc:
            errors.append(f"读取笔记失败: {exc}")
            continue
        for info in infos:
            if info and info.get("modelName"):
                infos_by_id[int(info["noteId"])] = info
        _notify(progress, "读取笔记", min(offset + 500, len(ordered)), len(ordered))

    orphans = []
    orphan_fronts: dict[int, str] = {}
    for note_id in sorted(infos_by_id, reverse=True):
        info = infos_by_id[note_id]
        if note_id in db_notes or info["modelName"] != model_name:
            continue
        fields = info.get("fields") or {}
        front_md = anki_to_markdown((fields.get("Front") or {}).get("value", ""))
        orphan_fronts[note_id] = normalize_front(front_md)
        orphans.append(
            {
                "note_id": note_id,
                "deck": note_decks.get(note_id, ""),
                "model": info["modelName"],
                "front": _note_preview(fields, "Front"),
                "back": _note_preview(fields, "Back"),
            }
        )

    if orphans:
        with closing(connect()) as conn:
            relations = _orphan_relations(conn, orphan_fronts)
        for item in orphans:
            item.update(relations.get(item["note_id"], {}))
    merged_total = sum(1 for item in orphans if item.get("relation") == "merge")

    db_ids = sorted(db_notes)
    present: set[int] = set()
    for offset in range(0, len(db_ids), 500):
        chunk = db_ids[offset : offset + 500]
        try:
            infos = client.call("notesInfo", notes=chunk)
        except AnkiError as exc:
            errors.append(f"核对关联失败: {exc}")
            continue
        for info in infos:
            if info and info.get("modelName"):
                present.add(int(info["noteId"]))
        _notify(progress, "核对关联", min(offset + 500, len(db_ids)), len(db_ids))

    missing = []
    for note_id in db_ids:
        if note_id in present:
            continue
        row = db_notes[note_id]
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
        "decks": ordered_decks,
        "model": model_name,
        "scanned_notes": len(infos_by_id),
        "system_notes": len(db_ids),
        "orphan_total": len(orphans),
        "merged_total": merged_total,
        "missing_total": len(missing),
        "orphans": orphans[:AUDIT_LIMIT],
        "missing": missing[:AUDIT_LIMIT],
        "errors": errors,
    }


def _orphan_relations(conn, fronts: dict[int, str]) -> dict[int, dict]:
    """为未关联的 note 找出来源合并记录和当前所属卡片。"""
    if not fronts:
        return {}

    rows = conn.execute(
        "SELECT id, action, payload, created_at FROM review_action "
        "WHERE undone_at IS NULL AND action IN ('merge', 'merge_cards') "
        "ORDER BY id"
    ).fetchall()

    merged_into: dict[int, int] = {}
    note_actions: dict[int, dict] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        kps = payload.get("kps") or []
        if len(kps) < 2:
            continue

        survivor_id = payload.get("survivor_id")
        if survivor_id is None:
            alive = [
                kp["id"]
                for kp in kps
                if conn.execute(
                    "SELECT 1 FROM knowledge_point WHERE id = ?", (kp["id"],)
                ).fetchone()
            ]
            survivor_id = alive[0] if len(alive) == 1 else None

        if survivor_id is not None:
            for kp in kps:
                if kp.get("id") != survivor_id:
                    merged_into[kp["id"]] = survivor_id

        for kp in kps:
            if survivor_id is not None and kp.get("id") == survivor_id:
                continue
            note_id = kp.get("anki_note_id")
            if note_id not in fronts:
                continue
            note_survivor = survivor_id
            if note_survivor is None and len(kps) == 2:
                other = next(item for item in kps if item is not kp)
                note_survivor = other.get("id")
            note_actions[int(note_id)] = {
                "merge_action_id": row["id"],
                "merge_at": row["created_at"],
                "source_front": (kp.get("front_md") or "")[:120],
                "survivor_id": note_survivor,
            }

    def card_info(card_id: int, relation: str, action: dict | None) -> dict:
        card = conn.execute(
            "SELECT kp.id, kp.front_md, kp.status, m.path AS module_path "
            "FROM knowledge_point kp "
            "LEFT JOIN module m ON m.id = kp.module_id "
            "WHERE kp.id = ?",
            (card_id,),
        ).fetchone()
        if card is None:
            return {}
        result = {
            "relation": relation,
            "card_id": card["id"],
            "card_front": (card["front_md"] or "")[:160],
            "card_status": card["status"],
            "module_path": card["module_path"] or "",
        }
        if action:
            result.update(
                merge_action_id=action["merge_action_id"],
                merge_at=action["merge_at"],
                source_front=action["source_front"],
            )
        return result

    result: dict[int, dict] = {}
    for note_id, front_norm in fronts.items():
        action = note_actions.get(note_id)
        if action:
            card_id = action["survivor_id"]
            seen: set[int] = set()
            while (
                card_id is not None
                and card_id in merged_into
                and card_id not in seen
            ):
                seen.add(card_id)
                card_id = merged_into[card_id]
            if card_id is not None:
                info = card_info(card_id, "merge", action)
                if info:
                    result[note_id] = info
                    continue
            result[note_id] = {
                "relation": "merge",
                "merge_action_id": action["merge_action_id"],
                "merge_at": action["merge_at"],
                "source_front": action["source_front"],
            }
            continue

        card = conn.execute(
            "SELECT id FROM knowledge_point WHERE front_norm = ? LIMIT 1",
            (front_norm,),
        ).fetchone()
        if card is not None:
            result[note_id] = card_info(card["id"], "front", None)

    return result


def _plain_text(text: str) -> str:
    text = MARKDOWN_IMG_RE.sub("", text)
    text = re.sub(r"[`*_#>\[\]()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_note_by_front(
    client: AnkiClient, deck: str, front: str
) -> int | None:
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


def _load_sync_rows(conn, only_ready: bool, linked_only: bool = False):
    where = []
    if only_ready:
        where.append("kp.status = 'ready'")
    if linked_only:
        where.append(
            "(kp.anki_note_id IS NOT NULL OR s.anki_note_id IS NOT NULL)"
        )
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"""
        SELECT kp.id, kp.front_md, kp.back_md, kp.status, kp.anki_note_id,
               m.path AS module_path,
               s.pushed_hash, s.pushed_deck,
               s.pushed_front_hash, s.pushed_back_hash,
               s.anki_note_id AS sync_note_id
        FROM knowledge_point kp
        LEFT JOIN module m ON m.id = kp.module_id
        LEFT JOIN anki_sync s ON s.kp_id = kp.id
        {clause}
        ORDER BY kp.id
        """
    ).fetchall()


def _fetch_notes(
    client: AnkiClient,
    note_ids: set[int],
    progress: Progress | None = None,
    phase: str = "读取 Anki",
) -> dict[int, dict]:
    ordered = sorted(note_ids)
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


def _ensure_deck(client: AnkiClient, decks: set[str], deck: str) -> None:
    if deck not in decks:
        client.call("createDeck", deck=deck)
        decks.add(deck)


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


def _upsert_conflict(
    conn,
    kp_id: int,
    note_id: int,
    field: str,
    base_hash: str | None,
    system_value: str,
    anki_value: str,
    system_hash: str,
    anki_hash: str,
    note: str,
) -> None:
    existing = conn.execute(
        "SELECT id FROM anki_conflict WHERE kp_id = ? AND field = ? AND status = 'pending'",
        (kp_id, field),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE anki_conflict SET anki_note_id = ?, base_hash = ?, system_value = ?, "
            "anki_value = ?, system_hash = ?, anki_hash = ?, note = ? WHERE id = ?",
            (
                note_id,
                base_hash,
                system_value,
                anki_value,
                system_hash,
                anki_hash,
                note,
                existing["id"],
            ),
        )
    else:
        conn.execute(
            "INSERT INTO anki_conflict"
            "(kp_id, anki_note_id, field, base_hash, system_value, anki_value, "
            "system_hash, anki_hash, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kp_id,
                note_id,
                field,
                base_hash,
                system_value,
                anki_value,
                system_hash,
                anki_hash,
                note,
                utc_now(),
            ),
        )


def _close_conflict(conn, kp_id: int, field: str) -> None:
    """两侧内容已一致时自动关闭遗留冲突。"""
    conn.execute(
        "UPDATE anki_conflict SET status = 'resolved', resolution = 'system', "
        "resolved_at = ?, note = COALESCE(note, '两侧内容已一致，自动关闭') "
        "WHERE kp_id = ? AND field = ? AND status = 'pending'",
        (utc_now(), kp_id, field),
    )


def _save_baseline(
    conn,
    kp_id: int,
    note_id: int,
    deck: str | None,
    front_base: str | None,
    back_base: str | None,
    combined: str | None,
    pushed_at: str | None = None,
) -> None:
    if combined is None:
        conn.execute(
            "INSERT INTO anki_sync"
            "(kp_id, anki_note_id, pushed_front_hash, pushed_back_hash, "
            "pushed_deck, last_error) VALUES (?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(kp_id) DO UPDATE SET anki_note_id = excluded.anki_note_id, "
            "pushed_front_hash = excluded.pushed_front_hash, "
            "pushed_back_hash = excluded.pushed_back_hash, "
            "pushed_deck = excluded.pushed_deck, last_error = NULL",
            (kp_id, note_id, front_base, back_base, deck),
        )
    else:
        conn.execute(
            "INSERT INTO anki_sync"
            "(kp_id, anki_note_id, pushed_hash, pushed_deck, pushed_at, "
            "pushed_front_hash, pushed_back_hash, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(kp_id) DO UPDATE SET anki_note_id = excluded.anki_note_id, "
            "pushed_hash = excluded.pushed_hash, "
            "pushed_deck = excluded.pushed_deck, "
            "pushed_at = excluded.pushed_at, "
            "pushed_front_hash = excluded.pushed_front_hash, "
            "pushed_back_hash = excluded.pushed_back_hash, last_error = NULL",
            (kp_id, note_id, combined, deck, pushed_at, front_base, back_base),
        )


def _field_action(sys_hash: str, anki_hash: str, base_hash: str) -> str:
    sys_changed = sys_hash != base_hash
    anki_changed = anki_hash != base_hash
    if not sys_changed and not anki_changed:
        return "none"
    if sys_changed and not anki_changed:
        return "push"
    if not sys_changed and anki_changed:
        return "pull"
    return "same" if sys_hash == anki_hash else "conflict"


def _action_for(sys_hash: str, anki_hash: str, base_hash: str | None) -> str:
    if base_hash is None:
        return "same" if sys_hash == anki_hash else "conflict"
    return _field_action(sys_hash, anki_hash, base_hash)


def _conflict_reason(
    field: str, sys_hash: str, anki_hash: str, base_hash: str | None, extra: str = ""
) -> str:
    if extra:
        return extra
    if base_hash is None:
        return "历史数据缺少字段基线，按安全模式记录"
    sys_changed = sys_hash != base_hash
    anki_changed = anki_hash != base_hash
    if sys_changed and anki_changed:
        return f"系统与 Anki 都修改了{FIELD_LABELS[field]}"
    if field == "front":
        return "Anki 修改了正面，未自动采纳"
    return "Anki 修改了背面"


def _sync_card(
    conn,
    client: AnkiClient,
    model_name: str,
    row,
    info: dict | None,
    mode: str,
    decks: set[str],
) -> tuple[dict, list[str]]:
    """按字段判定并执行推送/回写/记录冲突。mode 为 push 或 pull。"""
    kp_id = row["id"]
    note_id = row["anki_note_id"] or row["sync_note_id"]
    deck = _deck_of(row)
    front = row["front_md"] or ""
    back = row["back_md"] or ""
    result = {
        "changed": 0,
        "added": 0,
        "updated": 0,
        "pulled": 0,
        "conflicts": 0,
        "skipped": False,
    }
    errors: list[str] = []

    pending = {
        item["field"]
        for item in conn.execute(
            "SELECT field FROM anki_conflict WHERE kp_id = ? AND status = 'pending'",
            (kp_id,),
        )
    }
    if mode == "push" and pending:
        result["skipped"] = True
        return result, errors

    sys_front_hash = field_hash(front)
    sys_back_hash = field_hash(back)

    if note_id is None:
        if mode != "push":
            result["skipped"] = True
            return result, errors
        _ensure_deck(client, decks, deck)
        new_id = _add_note(
            client, model_name, {"deck": deck}, {"Front": front, "Back": back}
        )
        reused = False
        if new_id is None:
            new_id = _find_note_by_front(client, deck, front)
            if new_id is None:
                raise AnkiError("addNote 返回空且未找到同名笔记")
            reused = True
        now = utc_now()
        conn.execute(
            "UPDATE knowledge_point SET anki_note_id = ?, updated_at = ? WHERE id = ?",
            (new_id, now, kp_id),
        )
        result["changed"] = 1
        if reused:
            result["updated"] = 1
            _save_baseline(conn, kp_id, new_id, deck, sys_front_hash, None, None)
        else:
            result["added"] = 1
            _save_baseline(
                conn,
                kp_id,
                new_id,
                deck,
                sys_front_hash,
                sys_back_hash,
                _system_content_hash(front, back, deck),
                now,
            )
        return result, errors

    if info is None:
        if mode != "push":
            return result, [f"卡片 {kp_id}: Anki note {note_id} 不存在"]
        result["changed"] = 1
        reused = False
        try:
            client.call(
                "updateNoteFields",
                note={"id": note_id, "fields": {"Front": front, "Back": back}},
            )
            result["updated"] = 1
        except AnkiError as exc:
            if "not found" not in str(exc).lower():
                raise
            _ensure_deck(client, decks, deck)
            new_id = _add_note(
                client, model_name, {"deck": deck}, {"Front": front, "Back": back}
            )
            if new_id is None:
                new_id = _find_note_by_front(client, deck, front)
                if new_id is None:
                    raise exc
                reused = True
                result["updated"] = 1
            else:
                result["added"] = 1
            note_id = new_id
        conn.execute(
            "UPDATE knowledge_point SET anki_note_id = ?, updated_at = ? WHERE id = ?",
            (note_id, utc_now(), kp_id),
        )
        back_base = None if reused else sys_back_hash
        combined = (
            None
            if reused
            else _system_content_hash(front, back, deck)
        )
        _save_baseline(
            conn,
            kp_id,
            note_id,
            deck,
            sys_front_hash,
            back_base,
            combined,
            None if reused else utc_now(),
        )
        if row["pushed_deck"] != deck:
            _ensure_deck(client, decks, deck)
            _move_to_deck(client, note_id, deck)
        return result, errors

    anki_front = _anki_field(info, "Front")
    anki_back = _anki_field(info, "Back")
    anki_front_md = anki_to_markdown(anki_front)
    anki_back_md = anki_to_markdown(anki_back)
    anki_front_hash = field_hash(anki_front)
    anki_back_hash = field_hash(anki_back)

    base_f = row["pushed_front_hash"]
    base_b = row["pushed_back_hash"]
    if base_f is None or base_b is None:
        current_full = _system_content_hash(front, back, deck)
        if row["pushed_hash"] and row["pushed_hash"] == current_full:
            if base_f is None:
                base_f = sys_front_hash
            if base_b is None:
                base_b = sys_back_hash

    if "front" in pending:
        front_action = "same" if sys_front_hash == anki_front_hash else "conflict"
    else:
        front_action = _action_for(sys_front_hash, anki_front_hash, base_f)
    if "back" in pending:
        back_action = "same" if sys_back_hash == anki_back_hash else "conflict"
    else:
        back_action = _action_for(sys_back_hash, anki_back_hash, base_b)

    back_reason = ""
    if front_action == "pull":
        front_action = "conflict"
    if back_action == "pull":
        if not normalize_markdown(anki_back):
            back_action = "conflict"
            back_reason = "Anki 背面为空，未自动回写"
        else:
            media_errors = _pull_media(
                conn, client, local_media_refs(anki_back_md)
            )
            if media_errors:
                back_action = "conflict"
                back_reason = "；".join(media_errors)
            else:
                back = anki_back_md
                sys_back_hash = field_hash(back)
                conn.execute(
                    "UPDATE knowledge_point SET back_md = ?, updated_at = ? WHERE id = ?",
                    (back, utc_now(), kp_id),
                )
                base_b = sys_back_hash
                result["pulled"] = 1
                result["changed"] = 1

    fields: dict[str, str] = {}
    if front_action == "push":
        fields["Front"] = front
    if back_action == "push":
        fields["Back"] = back
    reused_note = False
    if fields:
        try:
            client.call(
                "updateNoteFields", note={"id": note_id, "fields": fields}
            )
        except AnkiError as exc:
            if "not found" not in str(exc).lower():
                raise
            _ensure_deck(client, decks, deck)
            new_id = _add_note(
                client, model_name, {"deck": deck}, {"Front": front, "Back": back}
            )
            if new_id is None:
                new_id = _find_note_by_front(client, deck, front)
                if new_id is None:
                    raise exc
                reused_note = True
            else:
                result["added"] = 1
            note_id = new_id
            conn.execute(
                "UPDATE knowledge_point SET anki_note_id = ?, updated_at = ? WHERE id = ?",
                (note_id, utc_now(), kp_id),
            )
            if reused_note:
                front_action = "same"
                back_action = "none"
            else:
                front_action = back_action = "push"
        result["updated"] = 1
        result["changed"] = 1

    if mode == "push" and row["pushed_deck"] != deck:
        _ensure_deck(client, decks, deck)
        _move_to_deck(client, note_id, deck)
        result["changed"] = 1

    if front_action == "conflict":
        _upsert_conflict(
            conn,
            kp_id,
            note_id,
            "front",
            base_f,
            front,
            anki_front_md,
            sys_front_hash,
            anki_front_hash,
            _conflict_reason("front", sys_front_hash, anki_front_hash, base_f),
        )
        result["conflicts"] += 1
        result["changed"] = 1
    else:
        _close_conflict(conn, kp_id, "front")

    if back_action == "conflict":
        _upsert_conflict(
            conn,
            kp_id,
            note_id,
            "back",
            base_b,
            back,
            anki_back_md,
            sys_back_hash,
            anki_back_hash,
            _conflict_reason(
                "back", sys_back_hash, anki_back_hash, base_b, back_reason
            ),
        )
        result["conflicts"] += 1
        result["changed"] = 1
    else:
        _close_conflict(conn, kp_id, "back")

    if front_action in ("push", "same"):
        base_f = sys_front_hash
    if back_action in ("push", "same"):
        base_b = sys_back_hash

    pending_left = conn.execute(
        "SELECT COUNT(*) AS value FROM anki_conflict "
        "WHERE kp_id = ? AND status = 'pending'",
        (kp_id,),
    ).fetchone()["value"]
    combined = None
    pushed_at = None
    if mode == "push" and pending_left == 0 and not reused_note:
        combined = _system_content_hash(front, back, deck)
        pushed_at = utc_now()
    if reused_note:
        base_b = None
    _save_baseline(
        conn,
        kp_id,
        note_id,
        deck if mode == "push" else row["pushed_deck"],
        base_f,
        base_b,
        combined,
        pushed_at,
    )
    return result, errors


def push(
    client: AnkiClient,
    model_name: str,
    sync_enabled: bool,
    progress: Progress | None = None,
) -> dict:
    _notify(progress, "校验 Anki", 0, 1)
    client.validate(model_name)
    _notify(progress, "校验 Anki", 1, 1)

    with closing(connect()) as conn:
        rows = _load_sync_rows(conn, only_ready=True)
        conflicted = {
            item["kp_id"]
            for item in conn.execute(
                "SELECT DISTINCT kp_id FROM anki_conflict WHERE status = 'pending'"
            )
        }

    linked_ids = {
        int(row["anki_note_id"] or row["sync_note_id"])
        for row in rows
        if (row["anki_note_id"] or row["sync_note_id"])
    }
    infos = _fetch_notes(client, linked_ids, progress)

    media_refs: set[str] = set()
    for row in rows:
        if row["id"] in conflicted:
            continue
        media_refs |= _markdown_media(row["front_md"], row["back_md"])
    media_refs = {name for name in media_refs if _is_local_media(name)}

    decks: set[str] = set(client.call("deckNames"))
    remote_media: set[str] = set()
    if media_refs:
        try:
            remote_media = set(client.call("getMediaFilesNames", pattern="*"))
        except AnkiError:
            remote_media = set()

    media_uploaded = 0
    errors: list[str] = []
    for name in sorted(media_refs):
        if name in remote_media:
            continue
        path = config.MEDIA_DIR / name
        if not path.is_file():
            errors.append(f"媒体缺失: {name}")
            continue
        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            client.call("storeMediaFile", filename=name, data=data)
            media_uploaded += 1
        except AnkiError as exc:
            errors.append(f"媒体 {name}: {exc}")

    added = updated = pulled = conflicts = skipped = failed = changed = 0
    with closing(connect()) as conn:
        for index, row in enumerate(rows, start=1):
            try:
                result, card_errors = _sync_card(
                    conn,
                    client,
                    model_name,
                    row,
                    infos.get(int(row["anki_note_id"] or row["sync_note_id"] or 0)),
                    "push",
                    decks,
                )
                conn.commit()
                added += result["added"]
                updated += result["updated"]
                pulled += result["pulled"]
                conflicts += result["conflicts"]
                changed += result["changed"]
                if result["skipped"]:
                    skipped += 1
                errors.extend(card_errors)
            except AnkiError as exc:
                failed += 1
                errors.append(f"卡片 {row['id']}: {exc}")
                conn.rollback()
                conn.execute(
                    "INSERT INTO anki_sync(kp_id, last_error) VALUES(?, ?) "
                    "ON CONFLICT(kp_id) DO UPDATE SET last_error = excluded.last_error",
                    (row["id"], str(exc)),
                )
                conn.commit()
            _notify(progress, "检查卡片", index, len(rows))

    backup = None
    sync_status = "skipped" if not changed else "disabled"
    if sync_enabled and changed:
        _notify(progress, "备份集合", 0, 1)
        backup = backup_anki()
        _notify(progress, "备份集合", 1, 1)

        sync_status = "ok"
        try:
            if media_uploaded:
                client.call("syncMedia", timeout=600)
            client.call("sync", timeout=600)
        except AnkiError as exc:
            sync_status = f"failed: {exc}"
            errors.append(f"同步失败: {exc}")

    return {
        "ready": len(rows),
        "changed": changed,
        "added": added,
        "updated": updated,
        "pulled": pulled,
        "conflicts": conflicts,
        "skipped_conflicts": skipped,
        "failed": failed,
        "media_uploaded": media_uploaded,
        "backup": backup,
        "sync": sync_status,
        "errors": errors,
    }


def pull(
    client: AnkiClient,
    model_name: str,
    progress: Progress | None = None,
) -> dict:
    """手动从 Anki 拉取：只回写 Anki 侧修改并记录冲突，不推送系统内容。"""
    _notify(progress, "校验 Anki", 0, 1)
    client.validate(model_name)
    _notify(progress, "校验 Anki", 1, 1)

    with closing(connect()) as conn:
        rows = _load_sync_rows(conn, only_ready=False, linked_only=True)
    note_ids = {int(row["anki_note_id"] or row["sync_note_id"]) for row in rows}
    infos = _fetch_notes(client, note_ids, progress)

    pulled = conflicts = skipped = 0
    errors: list[str] = []
    with closing(connect()) as conn:
        for index, row in enumerate(rows, start=1):
            note_id = int(row["anki_note_id"] or row["sync_note_id"])
            try:
                result, card_errors = _sync_card(
                    conn, client, model_name, row, infos.get(note_id), "pull", set()
                )
                conn.commit()
                pulled += result["pulled"]
                conflicts += result["conflicts"]
                if result["skipped"]:
                    skipped += 1
                errors.extend(card_errors)
            except AnkiError as exc:
                conn.rollback()
                errors.append(f"卡片 {row['id']}: {exc}")
            _notify(progress, "拉取笔记", index, len(rows))

    return {
        "linked": len(rows),
        "checked": len(rows) - skipped,
        "skipped": skipped,
        "pulled": pulled,
        "conflicts": conflicts,
        "errors": errors,
    }


def _conflict_detail(conn, conflict_id: int) -> dict:
    row = conn.execute(
        """
        SELECT c.id, c.kp_id, c.anki_note_id, c.field, c.base_hash,
               c.system_value, c.anki_value, c.system_hash, c.anki_hash,
               c.status, c.resolution, c.note, c.created_at, c.resolved_at,
               kp.front_md, kp.back_md, kp.status AS card_status,
               m.path AS module_path
        FROM anki_conflict c
        JOIN knowledge_point kp ON kp.id = c.kp_id
        LEFT JOIN module m ON m.id = kp.module_id
        WHERE c.id = ?
        """,
        (conflict_id,),
    ).fetchone()
    if row is None:
        raise LookupError("冲突不存在")
    item = dict(row)
    item["field_label"] = FIELD_LABELS.get(item["field"], item["field"])
    item["front_preview"] = _plain_text(item["front_md"] or "")[:160]
    return item


def list_conflicts(status: str = "pending", limit: int = 100) -> dict:
    if status not in ("pending", "resolved"):
        raise ValueError("不支持的冲突状态")
    limit = max(1, min(limit, 500))
    with closing(connect()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS value FROM anki_conflict WHERE status = ?",
            (status,),
        ).fetchone()["value"]
        rows = conn.execute(
            """
            SELECT c.id, c.kp_id, c.anki_note_id, c.field, c.base_hash,
                   c.system_value, c.anki_value, c.system_hash, c.anki_hash,
                   c.status, c.resolution, c.note, c.created_at, c.resolved_at,
                   kp.front_md, kp.status AS card_status, m.path AS module_path
            FROM anki_conflict c
            JOIN knowledge_point kp ON kp.id = c.kp_id
            LEFT JOIN module m ON m.id = kp.module_id
            WHERE c.status = ?
            ORDER BY c.id DESC LIMIT ?
            """,
            (status, limit),
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["field_label"] = FIELD_LABELS.get(item["field"], item["field"])
        item["front_preview"] = _plain_text(item["front_md"] or "")[:160]
        item.pop("front_md", None)
        items.append(item)
    return {"total": total, "status": status, "items": items}


def _card_content_hash(conn, kp_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT kp.front_md, kp.back_md, m.path AS module_path
        FROM knowledge_point kp
        LEFT JOIN module m ON m.id = kp.module_id
        WHERE kp.id = ?
        """,
        (kp_id,),
    ).fetchone()
    if row is None:
        return None
    deck = _deck_name(row["module_path"])
    return _system_content_hash(row["front_md"], row["back_md"], deck)


def _pending_count(conn, kp_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS value FROM anki_conflict "
        "WHERE kp_id = ? AND status = 'pending'",
        (kp_id,),
    ).fetchone()["value"]


def _refresh_combined(conn, kp_id: int) -> None:
    if _pending_count(conn, kp_id):
        return
    combined = _card_content_hash(conn, kp_id)
    conn.execute(
        "UPDATE anki_sync SET pushed_hash = ?, pushed_at = ? WHERE kp_id = ?",
        (combined, utc_now(), kp_id),
    )


def resolve_conflict(
    client: AnkiClient, conflict_id: int, choice: str
) -> dict:
    """用系统内容覆盖 Anki，或采用 Anki 背面回写系统。"""
    if choice not in ("system", "anki"):
        raise ValueError("不支持的解决方式")

    with closing(connect()) as conn, conn:
        conflict = conn.execute(
            "SELECT * FROM anki_conflict WHERE id = ?", (conflict_id,)
        ).fetchone()
        if conflict is None:
            raise LookupError("冲突不存在")
        if conflict["status"] != "pending":
            raise RuntimeError("该冲突已处理")
        if choice == "anki" and conflict["field"] != "back":
            raise ValueError("正面冲突只能使用系统内容")

        card = conn.execute(
            """
            SELECT kp.front_md, kp.back_md, kp.anki_note_id, m.path AS module_path
            FROM knowledge_point kp
            LEFT JOIN module m ON m.id = kp.module_id
            WHERE kp.id = ?
            """,
            (conflict["kp_id"],),
        ).fetchone()
        if card is None:
            raise LookupError("卡片不存在")

        note_id = conflict["anki_note_id"] or card["anki_note_id"]
        if note_id is None:
            raise RuntimeError("卡片未关联 Anki 笔记")

        if choice == "system":
            value = (
                card["front_md"] if conflict["field"] == "front" else card["back_md"]
            )
            client.call(
                "updateNoteFields",
                note={
                    "id": note_id,
                    "fields": {FIELD_NAMES[conflict["field"]]: value},
                },
            )
            column = (
                "pushed_front_hash"
                if conflict["field"] == "front"
                else "pushed_back_hash"
            )
            conn.execute(
                f"UPDATE anki_sync SET {column} = ? WHERE kp_id = ?",
                (field_hash(value), conflict["kp_id"]),
            )
            conn.execute(
                "UPDATE anki_conflict SET status = 'resolved', resolution = 'system', "
                "resolved_at = ? WHERE id = ?",
                (utc_now(), conflict_id),
            )
            _refresh_combined(conn, conflict["kp_id"])
            return _conflict_detail(conn, conflict_id)

        value = conflict["anki_value"]
        media_errors = _pull_media(conn, client, local_media_refs(value))
        if media_errors:
            note = "；".join(media_errors)
            conn.execute(
                "UPDATE anki_conflict SET note = ? WHERE id = ?",
                (note, conflict_id),
            )
            conn.commit()
            raise AnkiError(note)

        previous_back = card["back_md"] or ""
        previous_base = conn.execute(
            "SELECT pushed_back_hash FROM anki_sync WHERE kp_id = ?",
            (conflict["kp_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE knowledge_point SET back_md = ?, updated_at = ? WHERE id = ?",
            (value, utc_now(), conflict["kp_id"]),
        )
        conn.execute(
            "UPDATE anki_sync SET pushed_back_hash = ? WHERE kp_id = ?",
            (field_hash(value), conflict["kp_id"]),
        )
        conn.execute(
            "UPDATE anki_conflict SET status = 'resolved', resolution = 'anki', "
            "system_value = ?, base_hash = ?, resolved_at = ? WHERE id = ?",
            (
                previous_back,
                previous_base["pushed_back_hash"] if previous_base else None,
                utc_now(),
                conflict_id,
            ),
        )
        _refresh_combined(conn, conflict["kp_id"])
        return _conflict_detail(conn, conflict_id)


def undo_conflict(conflict_id: int) -> dict:
    """撤销「用 Anki」，恢复系统原背面并重新打开冲突。"""
    with closing(connect()) as conn, conn:
        conflict = conn.execute(
            "SELECT * FROM anki_conflict WHERE id = ?", (conflict_id,)
        ).fetchone()
        if conflict is None:
            raise LookupError("冲突不存在")
        if conflict["status"] != "resolved" or conflict["resolution"] != "anki":
            raise RuntimeError("只有「用 Anki」的解决可以撤销")

        conn.execute(
            "DELETE FROM anki_conflict WHERE kp_id = ? AND field = ? "
            "AND status = 'pending' AND id != ?",
            (conflict["kp_id"], conflict["field"], conflict_id),
        )
        conn.execute(
            "UPDATE knowledge_point SET back_md = ?, updated_at = ? WHERE id = ?",
            (conflict["system_value"], utc_now(), conflict["kp_id"]),
        )
        conn.execute(
            "UPDATE anki_sync SET pushed_back_hash = ?, pushed_hash = NULL "
            "WHERE kp_id = ?",
            (conflict["base_hash"], conflict["kp_id"]),
        )
        conn.execute(
            "UPDATE anki_conflict SET status = 'pending', resolution = NULL, "
            "resolved_at = NULL, system_hash = ?, note = ? WHERE id = ?",
            (
                field_hash(conflict["system_value"]),
                "已撤销「用 Anki」，冲突重新打开。",
                conflict_id,
            ),
        )
        return _conflict_detail(conn, conflict_id)


def _add_note(
    client: AnkiClient, model_name: str, item: dict, fields: dict
) -> int | None:
    return client.call(
        "addNote",
        note={
            "deckName": item["deck"],
            "modelName": model_name,
            "fields": fields,
            "tags": [],
            "options": {
                "allowDuplicate": False,
                "duplicateScope": "deck",
            },
        },
    )


def _move_to_deck(client: AnkiClient, note_id: int, deck: str) -> None:
    cards = client.call("findCards", query=f"nid:{note_id}")
    if cards:
        client.call("changeDeck", cards=cards, deck=deck)


def sync_anki(
    client: AnkiClient,
    model_name: str,
    sync_enabled: bool,
    progress: Progress | None = None,
) -> dict:
    _notify(progress, "校验 Anki", 0, 1)
    client.validate(model_name)
    _notify(progress, "校验 Anki", 1, 1)

    if not sync_enabled:
        return {"backup": None, "sync": "disabled"}

    _notify(progress, "备份集合", 0, 1)
    backup = backup_anki()
    _notify(progress, "备份集合", 1, 1)
    _notify(progress, "同步 AnkiWeb", 0, 1)
    client.call("sync", timeout=600)
    client.call("syncMedia", timeout=600)
    _notify(progress, "同步 AnkiWeb", 1, 1)
    return {"backup": backup, "sync": "ok"}


def summary() -> dict:
    """本地同步概况：状态计数、待推送数量、最近推送与失败。"""
    with closing(connect()) as conn:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM knowledge_point GROUP BY status"
            )
        }
        rows = conn.execute(
            """
            SELECT kp.front_md, kp.back_md, m.path AS module_path, s.pushed_hash
            FROM knowledge_point kp
            LEFT JOIN module m ON m.id = kp.module_id
            LEFT JOIN anki_sync s ON s.kp_id = kp.id
            WHERE kp.status = 'ready'
            """
        ).fetchall()
        matched = conn.execute(
            "SELECT COUNT(*) AS value FROM knowledge_point WHERE anki_note_id IS NOT NULL"
        ).fetchone()["value"]
        last_push = conn.execute(
            "SELECT MAX(pushed_at) AS value FROM anki_sync"
        ).fetchone()["value"]
        failures = [
            {"kp_id": row["kp_id"], "error": row["last_error"]}
            for row in conn.execute(
                "SELECT kp_id, last_error FROM anki_sync "
                "WHERE last_error IS NOT NULL ORDER BY kp_id LIMIT 10"
            )
        ]
        conflicts = conn.execute(
            "SELECT COUNT(*) AS value FROM anki_conflict WHERE status = 'pending'"
        ).fetchone()["value"]

    pending = 0
    for row in rows:
        deck = _deck_name(row["module_path"])
        media = _markdown_media(row["front_md"], row["back_md"])
        if _content_hash(row["front_md"], row["back_md"], deck, media) != row["pushed_hash"]:
            pending += 1

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
    }
