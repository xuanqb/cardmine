"""接管匹配、增量推送与同步。"""

import base64
import hashlib
import json
import re
from contextlib import closing
from typing import Callable

from .. import config
from ..db import connect, utc_now
from ..importer.textfmt import anki_to_markdown, normalize_front
from .backup import backup_anki
from .client import AnkiClient, AnkiError

Progress = Callable[[str, int, int], None]
AUDIT_LIMIT = 500
MARKDOWN_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
REMOTE_PREFIXES = ("http://", "https://", "//", "/")


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
        rows = conn.execute(
            """
            SELECT kp.id, kp.front_md, kp.back_md, kp.anki_note_id,
                   m.path AS module_path,
                   s.pushed_hash, s.pushed_deck,
                   s.anki_note_id AS sync_note_id
            FROM knowledge_point kp
            LEFT JOIN module m ON m.id = kp.module_id
            LEFT JOIN anki_sync s ON s.kp_id = kp.id
            WHERE kp.status = 'ready'
            ORDER BY kp.id
            """
        ).fetchall()

    changed = []
    for row in rows:
        deck = _deck_name(row["module_path"])
        media = _markdown_media(row["front_md"], row["back_md"])
        digest = _content_hash(row["front_md"], row["back_md"], deck, media)
        if digest != row["pushed_hash"]:
            changed.append(
                {
                    "id": row["id"],
                    "front": row["front_md"],
                    "back": row["back_md"],
                    "deck": deck,
                    "media": media,
                    "anki_note_id": row["anki_note_id"] or row["sync_note_id"],
                    "pushed_deck": row["pushed_deck"],
                    "hash": digest,
                }
            )

    if not changed:
        return {
            "ready": len(rows),
            "changed": 0,
            "added": 0,
            "updated": 0,
            "failed": 0,
            "media_uploaded": 0,
            "backup": None,
            "sync": "skipped",
            "errors": [],
        }

    decks = set(client.call("deckNames"))
    media_refs: set[str] = set()
    for item in changed:
        media_refs |= {name for name in item["media"] if _is_local_media(name)}

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

    added = updated = failed = 0
    with closing(connect()) as conn:
        for index, item in enumerate(changed, start=1):
            try:
                if item["deck"] not in decks:
                    client.call("createDeck", deck=item["deck"])
                    decks.add(item["deck"])

                fields = {"Front": item["front"], "Back": item["back"]}
                if item["anki_note_id"]:
                    try:
                        client.call(
                            "updateNoteFields",
                            note={"id": item["anki_note_id"], "fields": fields},
                        )
                        note_id = item["anki_note_id"]
                    except AnkiError as exc:
                        if "not found" not in str(exc).lower():
                            raise
                        # 笔记已被删除：清空 id 后重建
                        note_id = _add_note(client, model_name, item, fields)
                        if note_id is None:
                            raise exc
                        added += 1
                    else:
                        updated += 1
                        if item["pushed_deck"] != item["deck"]:
                            _move_to_deck(client, note_id, item["deck"])
                else:
                    note_id = _add_note(client, model_name, item, fields)
                    if note_id is None:
                        # addNote 返回空：按正面文本接管已有笔记
                        note_id = _find_note_by_front(client, item["deck"], item["front"])
                        if note_id is None:
                            raise AnkiError("addNote 返回空且未找到同名笔记")
                        updated += 1
                    else:
                        added += 1

                now = utc_now()
                conn.execute(
                    "UPDATE knowledge_point SET anki_note_id = ?, updated_at = ? WHERE id = ?",
                    (note_id, now, item["id"]),
                )
                conn.execute(
                    "INSERT INTO anki_sync"
                    "(kp_id, anki_note_id, pushed_hash, pushed_deck, pushed_at, last_error) "
                    "VALUES (?, ?, ?, ?, ?, NULL) "
                    "ON CONFLICT(kp_id) DO UPDATE SET "
                    "anki_note_id = excluded.anki_note_id, "
                    "pushed_hash = excluded.pushed_hash, "
                    "pushed_deck = excluded.pushed_deck, "
                    "pushed_at = excluded.pushed_at, last_error = NULL",
                    (item["id"], note_id, item["hash"], item["deck"], now),
                )
                conn.commit()
            except AnkiError as exc:
                failed += 1
                errors.append(f"卡片 {item['id']}: {exc}")
                conn.execute(
                    "INSERT INTO anki_sync(kp_id, last_error) VALUES(?, ?) "
                    "ON CONFLICT(kp_id) DO UPDATE SET last_error = excluded.last_error",
                    (item["id"], str(exc)),
                )
                conn.commit()
            _notify(progress, "写入卡片", index, len(changed))

    backup = None
    sync_status = "disabled"
    if sync_enabled:
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
        "changed": len(changed),
        "added": added,
        "updated": updated,
        "failed": failed,
        "media_uploaded": media_uploaded,
        "backup": backup,
        "sync": sync_status,
        "errors": errors,
    }


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
        "failures": failures,
    }
