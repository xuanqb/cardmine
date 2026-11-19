from contextlib import closing

from fastapi import APIRouter

from ..db import connect

router = APIRouter(prefix="/api", tags=["modules"])


@router.get("/modules")
def list_modules() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, parent_id, name, path, sort, created_by FROM module"
        ).fetchall()
        counts = dict(
            conn.execute(
                "SELECT module_id, COUNT(*) FROM knowledge_point "
                "WHERE module_id IS NOT NULL GROUP BY module_id"
            )
        )

    nodes: dict[int, dict] = {
        row["id"]: {
            "id": row["id"],
            "parent_id": row["parent_id"],
            "name": row["name"],
            "path": row["path"],
            "sort": row["sort"],
            "created_by": row["created_by"],
            "card_count": counts.get(row["id"], 0),
            "children": [],
        }
        for row in rows
    }

    roots: list[dict] = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"])
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)

    def finish(node: dict) -> int:
        node["children"].sort(key=lambda item: (item["sort"], item["name"]))
        total = node["card_count"] + sum(finish(child) for child in node["children"])
        node["card_count"] = total
        return total

    roots.sort(key=lambda item: (item["sort"], item["name"]))
    for root in roots:
        finish(root)
    return roots
