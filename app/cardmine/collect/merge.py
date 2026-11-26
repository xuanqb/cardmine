"""知识点归并：精确匹配 → 向量检索 → LLM 复核 / 待确认队列。"""

import math
import re
import sqlite3
import struct

from .. import aliases, groups
from ..ai import AIClient, AIError
from ..db import utc_now
from ..importer.textfmt import normalize_front

try:
    import sqlite_vec
except ImportError:  # pragma: no cover - 依赖缺失时降级
    sqlite_vec = None


class VectorError(Exception):
    pass


VERIFY_SYSTEM = """你是题库去重助手。判断两个面试问题是否在问同一个知识点（问法可以不同）。
只输出 JSON：{"same": true 或 false, "reason": "简要中文理由"}"""


def _blob(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unblob(raw: bytes, dim: int) -> tuple[float, ...]:
    return struct.unpack(f"{dim}f", raw)


def _cosine(left, right) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    if norm == 0:
        return 0.0
    return dot / norm


def drop_vectors(conn, kp_id: int) -> None:
    """删除单卡向量，正面变更后由下次 backfill 按新文本重算。"""
    conn.execute("DELETE FROM kp_embedding WHERE kp_id = ?", (kp_id,))
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'vec_kp'"
    ).fetchone()
    if exists is None:
        return
    if sqlite_vec is not None:
        try:
            sqlite_vec.load(conn)
        except (sqlite3.Error, OSError):
            return
    try:
        conn.execute("DELETE FROM vec_kp WHERE kp_id = ?", (kp_id,))
    except sqlite3.Error:
        pass


class VectorStore:
    """优先使用 sqlite-vec 虚拟表，扩展不可用时退化为普通表 + 余弦计算。"""

    def __init__(self, conn, dim: int):
        self.conn = conn
        self.dim = dim
        self.kind = "table"
        if sqlite_vec is not None:
            try:
                self._ensure_vec_table()
                self.kind = "vec"
            except (VectorError, sqlite3.Error, AttributeError, OSError):
                self.kind = "table"
        self._ensure_table()

    def _ensure_vec_table(self) -> None:
        sqlite_vec.load(self.conn)
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'vec_kp'"
        ).fetchone()
        if row is not None:
            match = re.search(r"float\[(\d+)\]", row["sql"] or "")
            if match and int(match.group(1)) == self.dim:
                return
            self.conn.execute("DROP TABLE vec_kp")
        self.conn.execute(
            "CREATE VIRTUAL TABLE vec_kp USING vec0("
            f"kp_id INTEGER PRIMARY KEY, embedding float[{self.dim}] "
            "distance_metric=cosine)"
        )
        self.conn.commit()

    def _ensure_table(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS kp_embedding ("
            "kp_id INTEGER PRIMARY KEY REFERENCES knowledge_point(id) ON DELETE CASCADE, "
            "dim INTEGER NOT NULL, vector BLOB NOT NULL)"
        )
        self.conn.execute("DELETE FROM kp_embedding WHERE dim != ?", (self.dim,))
        self.conn.commit()

    def store(self, kp_id: int, vector: list[float]) -> None:
        if self.kind == "vec":
            self.conn.execute("DELETE FROM vec_kp WHERE kp_id = ?", (kp_id,))
            self.conn.execute(
                "INSERT INTO vec_kp(kp_id, embedding) VALUES(?, ?)",
                (kp_id, _blob(vector)),
            )
            return
        self.conn.execute(
            "INSERT INTO kp_embedding(kp_id, dim, vector) VALUES(?, ?, ?) "
            "ON CONFLICT(kp_id) DO UPDATE SET dim = excluded.dim, "
            "vector = excluded.vector",
            (kp_id, self.dim, _blob(vector)),
        )

    def missing(self, limit: int) -> list:
        if self.kind == "vec":
            return self.conn.execute(
                "SELECT id, front_md FROM knowledge_point "
                "WHERE id NOT IN (SELECT kp_id FROM vec_kp) LIMIT ?",
                (limit,),
            ).fetchall()
        return self.conn.execute(
            "SELECT id, front_md FROM knowledge_point "
            "WHERE id NOT IN (SELECT kp_id FROM kp_embedding WHERE dim = ?) "
            "LIMIT ?",
            (self.dim, limit),
        ).fetchall()

    def load_all(self) -> dict[int, tuple[float, ...]]:
        if self.kind == "vec":
            rows = self.conn.execute("SELECT kp_id, embedding FROM vec_kp")
            return {
                int(row["kp_id"]): _unblob(row["embedding"], self.dim) for row in rows
            }
        rows = self.conn.execute(
            "SELECT kp_id, vector FROM kp_embedding WHERE dim = ?", (self.dim,)
        )
        return {int(row["kp_id"]): _unblob(row["vector"], self.dim) for row in rows}

    def search(self, vector: list[float], k: int = 30) -> list[tuple[int, float]]:
        if self.kind == "vec":
            rows = self.conn.execute(
                "SELECT kp_id, distance FROM vec_kp WHERE embedding MATCH ? "
                "ORDER BY distance LIMIT ?",
                (_blob(vector), k),
            ).fetchall()
            return [
                (int(row["kp_id"]), 1.0 - float(row["distance"])) for row in rows
            ]

        scored = []
        for row in self.conn.execute(
            "SELECT kp_id, vector FROM kp_embedding WHERE dim = ?", (self.dim,)
        ):
            similarity = _cosine(vector, _unblob(row["vector"], self.dim))
            scored.append((int(row["kp_id"]), similarity))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:k]


def backfill_embeddings(
    conn, client: AIClient, store: VectorStore, batch: int = 64
) -> int:
    total = 0
    while True:
        rows = store.missing(batch)
        if not rows:
            break
        vectors = client.embed([row["front_md"][:2000] for row in rows])
        for row, vector in zip(rows, vectors):
            store.store(row["id"], vector)
        conn.commit()
        total += len(rows)
    return total


def _known_root(conn, value: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, name, path, created_by FROM module "
        "WHERE parent_id IS NULL AND (name = ? OR path = ?) LIMIT 1",
        (value, value),
    ).fetchone()


def ensure_module(
    conn, path: str, created_by: str = "ai", group: sqlite3.Row | None = None
) -> int | None:
    parts = [part.strip() for part in path.strip("/").split("/") if part.strip()][:3]
    if not parts:
        return None

    if group is not None:
        root_path = group["path"]
        if parts[0] in (group["name"], root_path) or _known_root(conn, parts[0]):
            parts = parts[1:]
        parts = ([root_path] + parts)[:3]
    elif created_by == "ai":
        root = _known_root(conn, parts[0])
        needs_root = (
            root is None
            or root["created_by"] == "ai"
            or root["name"] == "Default"
        )
        if needs_root:
            preferred = groups.main_group(conn)
            if preferred and preferred["name"] != "Default" and (
                root is None or preferred["id"] != root["id"]
            ):
                parts = [preferred["path"]] + parts
                parts = parts[:3]

    parent_id = None
    current = ""
    for part in parts:
        current = f"{current}/{part}" if current else part
        row = conn.execute(
            "SELECT id FROM module WHERE path = ?", (current,)
        ).fetchone()
        if row is not None:
            parent_id = row["id"]
            continue
        cursor = conn.execute(
            "INSERT INTO module(parent_id, name, path, created_by) VALUES(?,?,?,?)",
            (parent_id, part, current, created_by),
        )
        parent_id = cursor.lastrowid
    return parent_id


def _verify_same(client: AIClient, left: str, right: str) -> tuple[bool, str]:
    payload = client.chat_json(VERIFY_SYSTEM, f"问题A：{left}\n问题B：{right}", retries=2)
    return bool(payload.get("same")), str(payload.get("reason") or "")[:200]


class Merger:
    def __init__(self, conn, client: AIClient, settings: dict):
        self.conn = conn
        self.client = client
        self.high = float(settings["dup_high_threshold"])
        self.low = min(self.high, float(settings["dup_low_threshold"]))
        self.dim = int(settings["ai_embedding_dim"] or 1024)
        self.store: VectorStore | None = None
        self.vector_error: str | None = None
        self.alias_error: str | None = None
        if client.embeddings_configured:
            try:
                self.store = VectorStore(conn, self.dim)
            except sqlite3.Error as exc:
                self.vector_error = str(exc)

    @property
    def vector_ready(self) -> bool:
        return self.store is not None

    def backfill(self) -> int:
        if self.store is None:
            return 0
        try:
            total = backfill_embeddings(self.conn, self.client, self.store)
        except (AIError, sqlite3.Error) as exc:
            self.store = None
            self.vector_error = str(exc)
            return 0
        try:
            aliases.backfill_embeddings(self.conn, self.client, self.dim)
        except (AIError, sqlite3.Error) as exc:
            self.alias_error = str(exc)
        return total

    def resolve(
        self,
        front: str,
        module_id: int | None,
        mention_id: int,
        subtype: str | None,
        group_id: int | None = None,
    ) -> tuple[str, int | None]:
        norm = normalize_front(front)
        row = self.conn.execute(
            "SELECT id FROM knowledge_point WHERE front_norm = ? AND group_id IS ?",
            (norm, group_id),
        ).fetchone()
        if row is not None:
            return "attached", row["id"]

        if subtype == "personal":
            action, kp_id, _ = self._create(front, module_id, None, group_id)
            return action, kp_id

        alias_hit = aliases.find_exact(self.conn, norm, group_id)
        if alias_hit is not None:
            return "attached", alias_hit[1]

        if self.store is None:
            action, kp_id, _ = self._create(front, module_id, None, group_id)
            return action, kp_id

        try:
            vector = self.client.embed([front])[0]
        except AIError as exc:
            self.vector_error = str(exc)
            action, kp_id, _ = self._create(front, module_id, None, group_id)
            return action, kp_id

        try:
            hits = self.store.search(vector)
        except sqlite3.Error as exc:
            self.store = None
            self.vector_error = str(exc)
            action, kp_id, _ = self._create(front, module_id, vector, group_id)
            return action, kp_id

        merged_hits: dict[int, dict] = {}
        for kp_id, score in hits:
            if score < self.low:
                continue
            kp = self.conn.execute(
                "SELECT id, front_md, module_id, group_id FROM knowledge_point "
                "WHERE id = ?",
                (kp_id,),
            ).fetchone()
            if kp is None or kp["group_id"] != group_id:
                continue
            current = merged_hits.get(kp_id)
            if current is None or score > current["score"]:
                merged_hits[kp_id] = {
                    "score": score,
                    "front_md": kp["front_md"],
                    "alias": False,
                }

        try:
            alias_hits = aliases.search(self.conn, vector, self.dim)
        except sqlite3.Error as exc:
            self.alias_error = str(exc)
            alias_hits = []

        for alias_row, score in alias_hits:
            if score < self.low:
                continue
            owner = aliases.resolve_owner(self.conn, alias_row["owner_kp_id"])
            if owner is None or alias_row["group_id"] != group_id:
                continue
            current = merged_hits.get(owner)
            if current is None or score > current["score"]:
                merged_hits[owner] = {
                    "score": score,
                    "front_md": alias_row["front_md"],
                    "alias": True,
                }

        best_middle: tuple[int, float, str] | None = None
        for kp_id, hit in sorted(
            merged_hits.items(), key=lambda item: item[1]["score"], reverse=True
        ):
            score = hit["score"]
            source_front = hit["front_md"]
            if hit["alias"]:
                reason = (
                    f"与已合并来源「{source_front[:60]}」相似度 {score:.2f}"
                )
            else:
                reason = f"相似度 {score:.2f}"
            if score >= self.high and self.client.configured:
                try:
                    same, ai_reason = _verify_same(self.client, front, source_front)
                except AIError as exc:
                    same, ai_reason = False, f"LLM 复核失败: {exc}"
                if ai_reason:
                    reason = f"{reason}；{ai_reason}" if hit["alias"] else ai_reason
                if same:
                    return "attached", kp_id
            if best_middle is None:
                best_middle = (kp_id, score, reason)

        if best_middle is not None:
            left_kp_id, score, reason = best_middle
            try:
                kp_id, created = self._create_kp(front, module_id, vector, group_id)
            except sqlite3.Error as exc:
                self.vector_error = str(exc)
                return "failed", None
            if not created:
                return "attached", kp_id
            self.conn.execute(
                "INSERT INTO dup_candidate"
                "(left_kp_id, right_kp_id, mention_id, score, reason, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (left_kp_id, kp_id, mention_id, score, reason, utc_now()),
            )
            return "candidate", kp_id

        action, kp_id, _ = self._create(front, module_id, vector, group_id)
        return action, kp_id

    def _create(
        self,
        front: str,
        module_id: int | None,
        vector: list[float] | None,
        group_id: int | None = None,
    ) -> tuple[str, int | None, bool]:
        try:
            kp_id, created = self._create_kp(front, module_id, vector, group_id)
        except sqlite3.Error as exc:
            self.vector_error = str(exc)
            return "failed", None, False
        return ("created" if created else "attached"), kp_id, created

    def _create_kp(
        self,
        front: str,
        module_id: int | None,
        vector: list[float] | None,
        group_id: int | None = None,
    ) -> tuple[int, bool]:
        now = utc_now()
        norm = normalize_front(front)
        try:
            cursor = self.conn.execute(
                "INSERT INTO knowledge_point"
                "(front_md, back_md, front_norm, module_id, group_id, status, origin, "
                " created_at, updated_at) "
                "VALUES(?, '', ?, ?, ?, 'draft', 'crawl', ?, ?)",
                (front, norm, module_id, group_id, now, now),
            )
            kp_id = cursor.lastrowid
            created = True
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                "SELECT id FROM knowledge_point WHERE front_norm = ? AND group_id IS ?",
                (norm, group_id),
            ).fetchone()
            if row is None:
                raise
            return int(row["id"]), False

        if self.store is not None and vector:
            try:
                self.store.store(kp_id, vector)
            except sqlite3.Error as exc:
                self.store = None
                self.vector_error = str(exc)
        return kp_id, created
