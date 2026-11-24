"""组（Group）：顶层模块，用于隔离采集、去重与导入。"""


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_group(conn, group_id: int | None):
    """返回顶层模块行；group_id 为空或不是顶层模块时返回 None。"""
    if group_id is None:
        return None
    return conn.execute(
        "SELECT id, parent_id, name, path FROM module "
        "WHERE id = ? AND parent_id IS NULL",
        (group_id,),
    ).fetchone()


def root_of(conn, module_id: int | None) -> int | None:
    """沿 parent_id 向上找到卡片所在模块的顶层模块。"""
    current = module_id
    for _ in range(64):
        if current is None:
            return None
        row = conn.execute(
            "SELECT id, parent_id FROM module WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            return None
        if row["parent_id"] is None:
            return int(row["id"])
        current = row["parent_id"]
    return None


def subtree_paths(conn, group_id: int | None) -> list[str]:
    """组内所有模块路径；未分组时返回空列表。"""
    group = get_group(conn, group_id)
    if group is None:
        return []
    rows = conn.execute(
        "SELECT path FROM module WHERE path = ? OR path LIKE ? ESCAPE '\\' "
        "ORDER BY path",
        (group["path"], _escape_like(group["path"]) + "/%"),
    ).fetchall()
    return [row["path"] for row in rows]


def main_group(conn):
    """现有卡片最多的非 AI 顶层模块，作为默认归属组。"""
    return conn.execute(
        """
        WITH RECURSIVE sub(root_id, id) AS (
            SELECT id, id FROM module WHERE parent_id IS NULL
            UNION ALL
            SELECT sub.root_id, m.id
            FROM module m JOIN sub ON m.parent_id = sub.id
        )
        SELECT m.id, m.name, m.path, COUNT(kp.id) AS cards
        FROM module m
        LEFT JOIN sub ON sub.root_id = m.id
        LEFT JOIN knowledge_point kp ON kp.module_id = sub.id
        WHERE m.parent_id IS NULL AND m.created_by != 'ai'
        GROUP BY m.id
        ORDER BY cards DESC, m.id
        LIMIT 1
        """
    ).fetchone()


def path_group_map(conn) -> dict[str, int | None]:
    """模块路径 -> 顶层组 id，用于导入时按目录树归组。"""
    rows = conn.execute("SELECT id, path FROM module").fetchall()
    by_path = {row["path"]: int(row["id"]) for row in rows}
    return {
        row["path"]: by_path.get(row["path"].split("/", 1)[0]) for row in rows
    }
