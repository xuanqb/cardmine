from contextlib import closing

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import connect

router = APIRouter(prefix="/api", tags=["modules"])


class ModuleIn(BaseModel):
    name: str


@router.post("/modules")
def create_group(payload: ModuleIn) -> dict:
    """新建一个空的顶层模块，即新的组。"""
    name = payload.name.strip()
    if not name or "/" in name:
        raise HTTPException(status_code=400, detail="组名不能为空且不能包含 /")
    if len(name) > 40:
        raise HTTPException(status_code=400, detail="组名过长")

    with closing(connect()) as conn, conn:
        if conn.execute(
            "SELECT 1 FROM module WHERE path = ?", (name,)
        ).fetchone():
            raise HTTPException(status_code=409, detail="同名模块已存在")
        sort = conn.execute(
            "SELECT COALESCE(MAX(sort), 0) + 1 AS value FROM module "
            "WHERE parent_id IS NULL"
        ).fetchone()["value"]
        cursor = conn.execute(
            "INSERT INTO module(parent_id, name, path, sort, created_by) "
            "VALUES(NULL, ?, ?, ?, 'manual')",
            (name, name, sort),
        )
    return {"id": cursor.lastrowid, "name": name, "path": name}


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
