"""卡片 AI 多轮对话：线程、消息、上下文与当前答案。"""

from contextlib import closing

from ..db import connect, utc_now
from . import answer

MAX_CONTEXT_MESSAGES = 20
MAX_TITLE_CHARS = 40


def title_from(text: str) -> str:
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return first[:MAX_TITLE_CHARS] or "新对话"


def _card_row(conn, card_id: int):
    return conn.execute(
        """
        SELECT kp.*, m.path AS module_path
        FROM knowledge_point kp
        LEFT JOIN module m ON m.id = kp.module_id
        WHERE kp.id = ?
        """,
        (card_id,),
    ).fetchone()


def _variants(conn, card_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT raw_text, section, context_md
        FROM mention WHERE kp_id = ?
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (card_id, answer.MAX_VARIANTS * 2),
    ).fetchall()
    return [dict(row) for row in rows]


def thread_dict(row, current_front_norm: str | None) -> dict:
    return {
        "id": row["id"],
        "kp_id": row["kp_id"],
        "title": row["title"] or "新对话",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": row["message_count"] if "message_count" in row.keys() else 0,
        "stale": bool(
            row["front_norm"]
            and current_front_norm
            and row["front_norm"] != current_front_norm
        ),
    }


def list_threads(card_id: int) -> list[dict]:
    with closing(connect()) as conn:
        card = _card_row(conn, card_id)
        if card is None:
            raise LookupError("卡片不存在")
        rows = conn.execute(
            """
            SELECT t.*,
                   (SELECT COUNT(*) FROM ai_message m WHERE m.thread_id = t.id)
                       AS message_count
            FROM ai_thread t
            WHERE t.kp_id = ?
            ORDER BY t.updated_at DESC, t.id DESC
            """,
            (card_id,),
        ).fetchall()
        return [thread_dict(row, card["front_norm"]) for row in rows]


def create_thread(card_id: int) -> dict:
    with closing(connect()) as conn, conn:
        card = _card_row(conn, card_id)
        if card is None:
            raise LookupError("卡片不存在")
        now = utc_now()
        cursor = conn.execute(
            "INSERT INTO ai_thread(kp_id, title, front_norm, created_at, updated_at) "
            "VALUES (?, '', ?, ?, ?)",
            (card_id, card["front_norm"], now, now),
        )
        return {
            "id": cursor.lastrowid,
            "kp_id": card_id,
            "title": "新对话",
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
            "stale": False,
        }


def get_thread(thread_id: int) -> dict:
    with closing(connect()) as conn:
        row = conn.execute(
            """
            SELECT t.*, kp.front_norm AS current_front_norm
            FROM ai_thread t JOIN knowledge_point kp ON kp.id = t.kp_id
            WHERE t.id = ?
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            raise LookupError("对话不存在")
        return {
            **thread_dict(row, row["current_front_norm"]),
            "current_front_norm": row["current_front_norm"],
        }


def delete_thread(thread_id: int) -> None:
    with closing(connect()) as conn, conn:
        cursor = conn.execute("DELETE FROM ai_thread WHERE id = ?", (thread_id,))
        if cursor.rowcount == 0:
            raise LookupError("对话不存在")


def list_messages(thread_id: int) -> list[dict]:
    with closing(connect()) as conn:
        if conn.execute(
            "SELECT 1 FROM ai_thread WHERE id = ?", (thread_id,)
        ).fetchone() is None:
            raise LookupError("对话不存在")
        rows = conn.execute(
            "SELECT id, role, content, status, created_at FROM ai_message "
            "WHERE thread_id = ? ORDER BY id",
            (thread_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def append_message(
    thread_id: int, role: str, content: str, status: str = "done"
) -> dict:
    with closing(connect()) as conn, conn:
        thread = conn.execute(
            "SELECT id, title FROM ai_thread WHERE id = ?", (thread_id,)
        ).fetchone()
        if thread is None:
            raise LookupError("对话不存在")
        now = utc_now()
        cursor = conn.execute(
            "INSERT INTO ai_message(thread_id, role, content, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (thread_id, role, content, status, now),
        )
        title = thread["title"]
        if role == "user" and not title:
            title = title_from(content)
            conn.execute(
                "UPDATE ai_thread SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, thread_id),
            )
        else:
            conn.execute(
                "UPDATE ai_thread SET updated_at = ? WHERE id = ?", (now, thread_id)
            )
        return {
            "id": cursor.lastrowid,
            "thread_id": thread_id,
            "role": role,
            "content": content,
            "status": status,
            "created_at": now,
        }


def _history(conn, thread_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM ai_message WHERE thread_id = ? "
        "AND status = 'done' ORDER BY id DESC LIMIT ?",
        (thread_id, MAX_CONTEXT_MESSAGES),
    ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def build_messages(settings: dict, thread_id: int) -> list[dict]:
    """系统提示 + 当前卡片上下文 + 最近 20 条已完成消息。"""
    with closing(connect()) as conn:
        thread = conn.execute(
            "SELECT id, kp_id FROM ai_thread WHERE id = ?", (thread_id,)
        ).fetchone()
        if thread is None:
            raise LookupError("对话不存在")
        card = _card_row(conn, thread["kp_id"])
        if card is None:
            raise LookupError("卡片不存在")
        variants = _variants(conn, thread["kp_id"])
        history = _history(conn, thread_id)

    system = (
        answer.build_system_prompt(answer.profile_from_settings(settings))
        + "\n\n以下是当前卡片的信息，回答时以它为准：\n"
        + answer.build_user_prompt(dict(card), variants)
        + "\n\n现在用户会围绕这道题继续追问或要求改写，请直接回答，不要重复题目。"
    )
    return [{"role": "system", "content": system}, *history]


def client_for(settings: dict):
    return answer._client(settings)


def latest_user_message(thread_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT id, role, content, status, created_at FROM ai_message "
            "WHERE thread_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return dict(row) if row else None


def set_answer(thread_id: int, message_id: int) -> int:
    """把某条已完成的 AI 回复设为当前答案，返回卡片 id。"""
    with closing(connect()) as conn, conn:
        row = conn.execute(
            """
            SELECT m.id, m.role, m.status, m.content, m.created_at, t.kp_id
            FROM ai_message m JOIN ai_thread t ON t.id = m.thread_id
            WHERE m.id = ? AND m.thread_id = ?
            """,
            (message_id, thread_id),
        ).fetchone()
        if row is None:
            raise LookupError("消息不存在")
        if row["role"] != "assistant":
            raise ValueError("只能把 AI 回复设为当前答案")
        if row["status"] != "done":
            raise ValueError("未完成的回复不能设为当前答案")
        card = conn.execute(
            "SELECT front_norm FROM knowledge_point WHERE id = ?", (row["kp_id"],)
        ).fetchone()
        if card is None:
            raise LookupError("卡片不存在")
        conn.execute(
            "UPDATE knowledge_point SET ai_back_md = ?, ai_status = 'done', "
            "ai_error = NULL, ai_generated_at = ?, ai_front_norm = ?, "
            "ai_answer_message_id = ? WHERE id = ?",
            (
                row["content"],
                row["created_at"],
                card["front_norm"],
                message_id,
                row["kp_id"],
            ),
        )
        return int(row["kp_id"])
