"""合并来源正面别名：记录、归属追踪、匹配与向量回填。"""

import json
import math
import struct

from .ai import AIError
from .db import utc_now
from .importer.textfmt import normalize_front

MAX_CHAIN = 32


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


def _field(row, key: str, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _kp_exists(conn, kp_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM knowledge_point WHERE id = ?", (kp_id,)).fetchone()
        is not None
    )


def resolve_owner(conn, kp_id: int | None) -> int | None:
    """沿合并链找到别名的最终归属卡；链路断开或终点不存在时返回 None。"""
    if kp_id is None:
        return None
    current = int(kp_id)
    seen: set[int] = set()
    while True:
        if current in seen or len(seen) >= MAX_CHAIN:
            return None
        seen.add(current)
        row = conn.execute(
            "SELECT survivor_kp_id FROM kp_merge_edge WHERE source_kp_id = ?",
            (current,),
        ).fetchone()
        if row is None:
            break
        current = int(row["survivor_kp_id"])
    if not _kp_exists(conn, current):
        return None
    return current


def record_merge(conn, action_id: int, survivor_id: int, sources) -> None:
    """把被合并卡片的正面写成别名，并记录来源卡 -> 存活卡的合并链。"""
    now = utc_now()
    owner = conn.execute(
        "SELECT group_id FROM knowledge_point WHERE id = ?", (survivor_id,)
    ).fetchone()
    group_id = owner["group_id"] if owner is not None else None
    for source in sources:
        source_id = int(_field(source, "id"))
        if source_id == survivor_id:
            continue
        front_md = (_field(source, "front_md") or "").strip()
        if not front_md:
            continue
        front_norm = _field(source, "front_norm") or normalize_front(front_md)
        module_id = _field(source, "module_id")
        existing = conn.execute(
            "SELECT id, front_md FROM kp_alias WHERE front_norm = ? AND group_id IS ?",
            (front_norm, group_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO kp_alias"
                "(owner_kp_id, front_md, front_norm, module_id, group_id, "
                " merge_action_id, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (survivor_id, front_md, front_norm, module_id, group_id, action_id, now),
            )
        elif existing["front_md"] != front_md:
            conn.execute(
                "UPDATE kp_alias SET owner_kp_id = ?, front_md = ?, module_id = ?, "
                "group_id = ?, merge_action_id = ?, embedding = NULL, embed_dim = NULL "
                "WHERE id = ?",
                (survivor_id, front_md, module_id, group_id, action_id, existing["id"]),
            )
        else:
            conn.execute(
                "UPDATE kp_alias SET owner_kp_id = ?, module_id = ?, group_id = ?, "
                "merge_action_id = ? WHERE id = ?",
                (survivor_id, module_id, group_id, action_id, existing["id"]),
            )
        conn.execute(
            "INSERT INTO kp_merge_edge"
            "(source_kp_id, survivor_kp_id, action_id, created_at) "
            "VALUES(?, ?, ?, ?) "
            "ON CONFLICT(source_kp_id) DO UPDATE SET "
            "survivor_kp_id = excluded.survivor_kp_id, "
            "action_id = excluded.action_id, created_at = excluded.created_at",
            (source_id, survivor_id, action_id, now),
        )


def remove_merge(conn, action_id: int) -> None:
    """撤销合并时删除该次合并产生的别名和合并链。"""
    conn.execute("DELETE FROM kp_alias WHERE merge_action_id = ?", (action_id,))
    conn.execute("DELETE FROM kp_merge_edge WHERE action_id = ?", (action_id,))


def find_exact(
    conn, front_norm: str, group_id: int | None = None
) -> tuple[object, int] | None:
    row = conn.execute(
        "SELECT * FROM kp_alias WHERE front_norm = ? AND group_id IS ?",
        (front_norm, group_id),
    ).fetchone()
    if row is None:
        return None
    owner = resolve_owner(conn, row["owner_kp_id"])
    if owner is None:
        return None
    return row, owner


def search(conn, vector: list[float], dim: int, k: int = 30) -> list[tuple[object, float]]:
    """别名向量数量通常远小于卡片数，直接线性计算余弦相似度。"""
    rows = conn.execute(
        "SELECT * FROM kp_alias WHERE embed_dim = ? AND embedding IS NOT NULL",
        (dim,),
    ).fetchall()
    scored = [
        (row, _cosine(vector, _unblob(row["embedding"], dim))) for row in rows
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:k]


def load_all(conn, dim: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM kp_alias WHERE embed_dim = ? AND embedding IS NOT NULL",
        (dim,),
    ).fetchall()
    items: list[dict] = []
    for row in rows:
        item = dict(row)
        item["vector"] = _unblob(row["embedding"], dim)
        items.append(item)
    return items


def backfill_embeddings(conn, client, dim: int, batch: int = 64) -> int:
    conn.execute(
        "UPDATE kp_alias SET embedding = NULL, embed_dim = NULL "
        "WHERE embed_dim IS NOT NULL AND embed_dim != ?",
        (dim,),
    )
    total = 0
    while True:
        rows = conn.execute(
            "SELECT id, front_md FROM kp_alias WHERE embedding IS NULL LIMIT ?",
            (batch,),
        ).fetchall()
        if not rows:
            break
        vectors = client.embed([row["front_md"][:2000] for row in rows])
        if len(vectors) != len(rows):
            raise AIError(
                f"别名向量数量不匹配：期望 {len(rows)}，实际 {len(vectors)}"
            )
        for row, vector in zip(rows, vectors):
            if len(vector) != dim:
                raise AIError(
                    f"别名向量维度不匹配：期望 {dim}，实际 {len(vector)}"
                )
            conn.execute(
                "UPDATE kp_alias SET embed_dim = ?, embedding = ? WHERE id = ?",
                (dim, _blob(vector), row["id"]),
            )
        conn.commit()
        total += len(rows)
    return total


def list_for_survivor(conn, survivor_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT a.id, a.owner_kp_id, a.front_md, a.merge_action_id, a.created_at, "
        "m.path AS module_path "
        "FROM kp_alias a LEFT JOIN module m ON m.id = a.module_id "
        "ORDER BY a.id"
    ).fetchall()
    edges = {
        int(row["source_kp_id"]): int(row["survivor_kp_id"])
        for row in conn.execute(
            "SELECT source_kp_id, survivor_kp_id FROM kp_merge_edge"
        )
    }
    cache: dict[int, int | None] = {}

    def owner_of(kp_id: int) -> int | None:
        if kp_id in cache:
            return cache[kp_id]
        current = kp_id
        seen: set[int] = set()
        while current in edges and current not in seen and len(seen) < MAX_CHAIN:
            seen.add(current)
            current = edges[current]
        if current in seen or not _kp_exists(conn, current):
            cache[kp_id] = None
            return None
        cache[kp_id] = current
        return current

    items: list[dict] = []
    for row in rows:
        if owner_of(int(row["owner_kp_id"])) != survivor_id:
            continue
        items.append(
            {
                "id": row["id"],
                "front_md": row["front_md"],
                "module_path": row["module_path"],
                "merge_action_id": row["merge_action_id"],
                "created_at": row["created_at"],
            }
        )
    return items


def delete_for_card(conn, survivor_id: int, alias_id: int) -> bool:
    row = conn.execute(
        "SELECT id, owner_kp_id FROM kp_alias WHERE id = ?", (alias_id,)
    ).fetchone()
    if row is None or resolve_owner(conn, row["owner_kp_id"]) != survivor_id:
        return False
    conn.execute("DELETE FROM kp_alias WHERE id = ?", (alias_id,))
    return True


def _payload_ids(payload: dict) -> set[int]:
    ids: set[int] = set()
    survivor_id = payload.get("survivor_id")
    if survivor_id:
        ids.add(int(survivor_id))
    for kp in payload.get("kps") or []:
        try:
            ids.add(int(kp["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def _infer_survivor(
    conn, action: str, payload: dict, later_ids: set[int]
) -> int | None:
    explicit = payload.get("survivor_id")
    if explicit:
        return int(explicit)
    if action != "merge":
        return None
    kps = payload.get("kps") or []
    ids = []
    for kp in kps:
        try:
            ids.append(int(kp["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(ids) != 2:
        return None
    alive = [kp_id for kp_id in ids if _kp_exists(conn, kp_id)]
    referenced = [kp_id for kp_id in ids if kp_id in later_ids]
    if len(referenced) == 1:
        return referenced[0]
    if len(alive) == 1:
        return alive[0]
    return None


def backfill_history(conn) -> int:
    """从历史合并记录补出别名和合并链，可重复执行。"""
    rows = conn.execute(
        "SELECT id, action, payload FROM review_action "
        "WHERE undone_at IS NULL AND action IN ('merge', 'merge_cards') "
        "ORDER BY id"
    ).fetchall()
    parsed: list[tuple[int, str, dict]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        parsed.append((int(row["id"]), row["action"], payload))

    later_refs: dict[int, set[int]] = {}
    suffix: set[int] = set()
    for action_id, _, payload in reversed(parsed):
        later_refs[action_id] = set(suffix)
        suffix.update(_payload_ids(payload))

    total = 0
    for action_id, action, payload in parsed:
        survivor_id = _infer_survivor(
            conn, action, payload, later_refs.get(action_id, set())
        )
        if survivor_id is None:
            continue
        sources = []
        for kp in payload.get("kps") or []:
            try:
                source_id = int(kp["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if source_id == survivor_id or _kp_exists(conn, source_id):
                continue
            sources.append(kp)
        if not sources:
            continue
        record_merge(conn, action_id, survivor_id, sources)
        total += len(sources)
    conn.commit()
    return total
