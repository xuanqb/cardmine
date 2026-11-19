"""导出 apkg / CSV，以及应用数据备份。"""

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .db import connect
from .importer.textfmt import markdown_to_anki

MODEL_ID = 1600000000001
MODEL_NAME = "Basic"

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)\s*\)")
_HTML_IMG_RE = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', re.I)
_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
_REMOTE_PREFIXES = ("http://", "https://", "//", "data:", "/")

_TEMPLATE_HEAD = """<div class="%s">{{%s}}</div>
<script src="markdown-it.min.js"></script>
<script src="highlight.min.js"></script>
<script>
(function () {
  if (typeof window.markdownit !== "function") return;
  var el = document.querySelector(".%s");
  if (!el) return;
  var md = window.markdownit({ html: true, linkify: true, breaks: false });
  el.innerHTML = md.render(el.innerHTML.replace(/<br\\s*\\/?>/gi, "\\n"));
  if (window.hljs) {
    el.querySelectorAll("pre code").forEach(function (block) {
      window.hljs.highlightElement(block);
    });
  }
})();
</script>"""

TEMPLATE_CSS = """@import url("https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.0/github-markdown.min.css");
@import url("https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css");

.card {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 16px;
  line-height: 1.7;
  text-align: left;
  color: #1f2328;
  background: #ffffff;
  padding: 12px;
}
.card img { max-width: 100%; }
.card pre {
  padding: 12px 14px;
  border-radius: 6px;
  background: #f6f8fa;
  overflow: auto;
}
.card code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
hr#answer { margin: 16px 0; border: none; border-top: 1px solid #d8dee4; }
"""


@dataclass
class ExportResult:
    path: str
    filename: str
    cards: int = 0
    media: int = 0
    missing_media: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        Path(self.path).unlink(missing_ok=True)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _module_ids(conn: sqlite3.Connection, module_id: int) -> list[int]:
    row = conn.execute("SELECT path FROM module WHERE id = ?", (module_id,)).fetchone()
    if row is None:
        return []
    path = row["path"]
    rows = conn.execute(
        "SELECT id FROM module WHERE path = ? OR path LIKE ? ESCAPE '\\'",
        (path, _like_escape(path) + "/%"),
    ).fetchall()
    return [item["id"] for item in rows]


def _select_cards(
    conn: sqlite3.Connection, module_id: int | None, status: str | None
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list = []
    if module_id is not None:
        ids = _module_ids(conn, module_id)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        where.append(f"kp.module_id IN ({placeholders})")
        params.extend(ids)
    if status:
        where.append("kp.status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"""
        SELECT kp.id, kp.front_md, kp.back_md, kp.status,
               m.path AS module_path
        FROM knowledge_point kp
        LEFT JOIN module m ON m.id = kp.module_id
        {clause}
        ORDER BY kp.id
        """,
        params,
    ).fetchall()


def _deck_name(module_path: str | None) -> str:
    """apkg 内的层级分隔符沿用 Anki 集合格式（C7：\\x1f）。"""
    return (module_path or "Default").replace("/", "\x1f")


def _csv_deck_name(module_path: str | None) -> str:
    return (module_path or "Default").replace("/", "::")


def local_media_refs(*texts: str) -> set[str]:
    refs: set[str] = set()
    for text in texts:
        if not text:
            continue
        refs.update(_MD_IMG_RE.findall(text))
        refs.update(_HTML_IMG_RE.findall(text))
        refs.update(_SOUND_RE.findall(text))
    return {name for name in refs if not name.startswith(_REMOTE_PREFIXES)}


def _checksum(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16)


def _guid(front_norm: str) -> str:
    return hashlib.sha1(front_norm.encode("utf-8")).hexdigest()[:16]


def _model_json(now_ms: int) -> dict:
    return {
        str(MODEL_ID): {
            "id": MODEL_ID,
            "name": MODEL_NAME,
            "type": 0,
            "mod": now_ms // 1000,
            "usn": 0,
            "sortf": 0,
            "did": 1,
            "tmpls": [
                {
                    "name": "Card 1",
                    "ord": 0,
                    "qfmt": _TEMPLATE_HEAD % ("front", "Front", "front"),
                    "afmt": "{{FrontSide}}\n<hr id=answer>\n"
                    + _TEMPLATE_HEAD % ("back", "Back", "back"),
                    "bqfmt": "",
                    "bafmt": "",
                    "did": None,
                    "bfont": "",
                    "bsize": 0,
                }
            ],
            "flds": [
                {
                    "name": "Front",
                    "ord": 0,
                    "sticky": False,
                    "rtl": False,
                    "font": "Arial",
                    "size": 20,
                    "media": [],
                },
                {
                    "name": "Back",
                    "ord": 1,
                    "sticky": False,
                    "rtl": False,
                    "font": "Arial",
                    "size": 20,
                    "media": [],
                },
            ],
            "css": TEMPLATE_CSS,
            "latexPre": "\\documentclass[12pt]{article}\n\\special{papersize=3in,5in}\n"
            "\\usepackage[utf8]{inputenc}\n\\usepackage{amssymb,amsmath}\n"
            "\\pagestyle{empty}\n\\setlength{\\parindent}{0in}\n\\begin{document}\n",
            "latexPost": "\\end{document}",
            "latexsvg": False,
            "req": [[0, "any", [0]]],
            "tags": [],
            "vers": [],
        }
    }


def _deck_json(deck_id: int, name: str, now_ms: int) -> dict:
    return {
        "id": deck_id,
        "name": name,
        "mod": now_ms // 1000,
        "usn": 0,
        "desc": "",
        "dyn": 0,
        "collapsed": False,
        "browserCollapsed": False,
        "newToday": [0, 0],
        "revToday": [0, 0],
        "lrnToday": [0, 0],
        "timeToday": [0, 0],
        "conf": 1,
        "extendNew": 10,
        "extendRev": 50,
    }


_DCONF = {
    "1": {
        "id": 1,
        "name": "Default",
        "mod": 0,
        "usn": 0,
        "maxTaken": 60,
        "autoplay": True,
        "timer": 0,
        "replayq": True,
        "dyn": False,
        "new": {
            "delays": [1, 10],
            "ints": [1, 4, 7],
            "initialFactor": 2500,
            "separate": True,
            "order": 1,
            "perDay": 20,
        },
        "rev": {
            "perDay": 200,
            "ease4": 1.3,
            "fuzz": 0.05,
            "minSpace": 1,
            "ivlFct": 1,
            "maxIvl": 36500,
            "bury": True,
            "hardFactor": 1.2,
        },
        "lapse": {
            "delays": [10],
            "mult": 0,
            "minInt": 1,
            "leechFails": 8,
            "leechAction": 0,
        },
        "conf": 1,
    }
}

_SCHEMA = """
CREATE TABLE col (
    id integer primary key, crt integer not null, mod integer not null,
    scm integer not null, ver integer not null, dty integer not null,
    usn integer not null, ls integer not null, conf text not null,
    models text not null, decks text not null, dconf text not null,
    tags text not null
);
CREATE TABLE notes (
    id integer primary key, guid text not null, mid integer not null,
    mod integer not null, usn integer not null, tags text not null,
    flds text not null, sfld integer not null, csum integer not null,
    flags integer not null, data text not null
);
CREATE TABLE cards (
    id integer primary key, nid integer not null, did integer not null,
    ord integer not null, mod integer not null, usn integer not null,
    type integer not null, queue integer not null, due integer not null,
    ivl integer not null, factor integer not null, reps integer not null,
    lapses integer not null, left integer not null, odue integer not null,
    odid integer not null, flags integer not null, data text not null
);
CREATE TABLE revlog (
    id integer primary key, cid integer not null, usn integer not null,
    ease integer not null, ivl integer not null, lastIvl integer not null,
    factor integer not null, time integer not null, type integer not null
);
CREATE TABLE graves (
    usn integer not null, oid integer not null, type integer not null
);
CREATE INDEX ix_notes_usn ON notes (usn);
CREATE INDEX ix_cards_usn ON cards (usn);
CREATE INDEX ix_revlog_usn ON revlog (usn);
CREATE INDEX ix_cards_nid ON cards (nid);
CREATE INDEX ix_cards_sched ON cards (did, queue, due);
CREATE INDEX ix_revlog_cid ON revlog (cid);
CREATE INDEX ix_notes_csum ON notes (csum);
"""


def _write_collection(
    db_path: Path,
    records: list[dict],
    deck_ids: dict[str, int],
    now_ms: int,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO col VALUES (1, ?, ?, ?, 11, 0, 0, 0, ?, ?, ?, ?, ?)",
            (
                now_ms // 1000,
                now_ms,
                now_ms,
                json.dumps(
                    {
                        "nextPos": 1,
                        "estTimes": True,
                        "activeDecks": [1],
                        "sortType": "noteFld",
                        "timeLim": 0,
                        "sortBackwards": False,
                        "addToCur": True,
                        "curDeck": 1,
                        "curModel": MODEL_ID,
                        "newSpread": 0,
                        "dueCounts": True,
                        "collapseTime": 1200,
                    }
                ),
                json.dumps(_model_json(now_ms), ensure_ascii=False),
                json.dumps(
                    {
                        "1": _deck_json(1, "Default", now_ms),
                        **{
                            str(deck_id): _deck_json(deck_id, name, now_ms)
                            for name, deck_id in deck_ids.items()
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(_DCONF, ensure_ascii=False),
                "{}",
            ),
        )

        note_base = now_ms * 1000
        for index, item in enumerate(records):
            note_id = note_base + index
            card_id = note_base + 1_000_000 + index
            conn.execute(
                "INSERT INTO notes VALUES (?, ?, ?, ?, 0, '', ?, ?, ?, 0, '')",
                (
                    note_id,
                    _guid(str(item["id"]) + item["front"]),
                    MODEL_ID,
                    now_ms // 1000,
                    "\x1f".join((item["front"], item["back"])),
                    item["sfld"],
                    item["csum"],
                ),
            )
            conn.execute(
                "INSERT INTO cards VALUES (?, ?, ?, 0, ?, 0, 0, 0, ?, 0, 0, 0, 0, 0, 0, 0, 0, '')",
                (
                    card_id,
                    note_id,
                    deck_ids[item["deck"]],
                    now_ms // 1000,
                    index + 1,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def build_apkg(module_id: int | None = None, status: str | None = None) -> ExportResult:
    with closing(connect()) as conn:
        rows = _select_cards(conn, module_id, status)

    if not rows:
        raise ValueError("没有符合条件的卡片可导出")

    now_ms = int(time.time() * 1000)
    records: list[dict] = []
    deck_names: list[str] = []
    media_refs: set[str] = set()

    for row in rows:
        deck = _deck_name(row["module_path"])
        if deck not in deck_names:
            deck_names.append(deck)
        refs = local_media_refs(row["front_md"], row["back_md"])
        media_refs |= refs
        front = markdown_to_anki(row["front_md"], convert_newlines=False)
        records.append(
            {
                "id": row["id"],
                "front": front,
                "back": markdown_to_anki(row["back_md"]),
                "deck": deck,
                "sfld": front,
                "csum": _checksum(front),
            }
        )

    media_files: dict[str, Path] = {}
    missing: list[str] = []
    for name in sorted(media_refs):
        path = config.MEDIA_DIR / name
        if path.is_file():
            media_files[name] = path
        else:
            missing.append(name)

    for name in ("markdown-it.min.js", "highlight.min.js"):
        source = config.WEB_DIR / "vendor" / name
        if source.is_file():
            media_files[name] = source
        else:
            missing.append(name)

    deck_ids: dict[str, int] = {}
    for name in deck_names:
        if name == "Default":
            deck_ids[name] = 1
            continue
        deck_id = 1600000000000 + int(
            hashlib.sha1(name.encode("utf-8")).hexdigest()[:10], 16
        ) % 100_000_000
        while deck_id in deck_ids.values() or deck_id == 1:
            deck_id += 1
        deck_ids[name] = deck_id

    workdir = Path(tempfile.mkdtemp(prefix="cardmine-export-"))
    try:
        collection = workdir / "collection.anki2"
        _write_collection(collection, records, deck_ids, now_ms)

        fd, out_path = tempfile.mkstemp(prefix="cardmine-", suffix=".apkg")
        os.close(fd)

        mapping: dict[str, str] = {}
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(collection, "collection.anki2")
            for index, (name, path) in enumerate(media_files.items()):
                zf.write(path, str(index))
                mapping[str(index)] = name
            zf.writestr("media", json.dumps(mapping, ensure_ascii=False))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    filename = time.strftime("cardmine-%Y%m%d-%H%M%S.apkg")
    return ExportResult(
        path=out_path,
        filename=filename,
        cards=len(records),
        media=len(media_files),
        missing_media=missing,
    )


def build_csv(module_id: int | None = None, status: str | None = None) -> tuple[str, int]:
    with closing(connect()) as conn:
        rows = _select_cards(conn, module_id, status)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Front", "Back", "Deck"])
    for row in rows:
        writer.writerow(
            [row["front_md"], row["back_md"], _csv_deck_name(row["module_path"])]
        )
    return "\ufeff" + buffer.getvalue(), len(rows)


def backup_database() -> tuple[str, str]:
    """使用 SQLite 在线备份 API 生成一致性快照。"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="cardmine-backup-", suffix=".db")
    os.close(fd)
    try:
        with closing(sqlite3.connect(config.DB_PATH, timeout=30)) as source:
            with closing(sqlite3.connect(path)) as target:
                source.backup(target)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    filename = time.strftime("cardmine-%Y%m%d-%H%M%S.db")
    return path, filename


def media_manifest_csv() -> str:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT filename, size, sha256, path, created_at FROM media ORDER BY filename"
        ).fetchall()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["filename", "size", "sha256", "path", "exists", "created_at"])
    for row in rows:
        exists = (config.MEDIA_DIR / row["path"]).is_file()
        writer.writerow(
            [row["filename"], row["size"], row["sha256"], row["path"], int(exists),
             row["created_at"]]
        )
    return "\ufeff" + buffer.getvalue()
