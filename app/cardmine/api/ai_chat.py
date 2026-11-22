"""卡片 AI 多轮对话接口：线程管理、SSE 流式回复与当前答案。"""

import asyncio
import json
import logging
import threading
from contextlib import closing

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..ai import AIError, chat
from ..db import connect
from .cards import get_card
from .settings import load_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ai-chat"])

_active_cards: set[int] = set()
_active_lock = threading.Lock()


class ThreadIn(BaseModel):
    title: str = ""


class MessageIn(BaseModel):
    content: str = Field(..., min_length=1)


def _reserve(card_id: int) -> None:
    with _active_lock:
        if card_id in _active_cards:
            raise HTTPException(status_code=409, detail="这张卡已有 AI 回复正在生成")
        _active_cards.add(card_id)


def _release(card_id: int) -> None:
    with _active_lock:
        _active_cards.discard(card_id)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _save_partial(thread_id: int, parts: list[str]) -> dict | None:
    content = "".join(parts).strip()
    if not content:
        return None
    try:
        return chat.append_message(thread_id, "assistant", content, "interrupted")
    except LookupError:
        return None


async def _stream_reply(thread_id: int, card_id: int, client, messages: list[dict]):
    parts: list[str] = []
    try:
        async for delta in client.chat_text_stream(messages):
            parts.append(delta)
            yield _sse("delta", {"text": delta})
        content = "".join(parts).strip()
        if not content:
            raise AIError("模型返回空内容")
        message = chat.append_message(thread_id, "assistant", content, "done")
        yield _sse("done", {"message": message})
    except (asyncio.CancelledError, GeneratorExit):
        _save_partial(thread_id, parts)
        raise
    except AIError as exc:
        message = _save_partial(thread_id, parts)
        payload = {"message": str(exc)}
        if message is not None:
            payload["message_id"] = message["id"]
        yield _sse("error", payload)
    except Exception as exc:  # noqa: BLE001 - 流式响应必须给出可读错误
        logger.exception("AI 对话生成失败")
        message = _save_partial(thread_id, parts)
        payload = {"message": f"生成失败：{exc}"}
        if message is not None:
            payload["message_id"] = message["id"]
        yield _sse("error", payload)
    finally:
        _release(card_id)


@router.get("/cards/{card_id}/ai-threads")
def list_threads(card_id: int) -> dict:
    try:
        return {"threads": chat.list_threads(card_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/cards/{card_id}/ai-threads")
def create_thread(card_id: int, payload: ThreadIn) -> dict:
    try:
        thread = chat.create_thread(card_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if payload.title.strip():
        with closing(connect()) as conn, conn:
            conn.execute(
                "UPDATE ai_thread SET title = ? WHERE id = ?",
                (payload.title.strip()[:60], thread["id"]),
            )
        thread["title"] = payload.title.strip()[:60]
    return {"thread": thread}


@router.delete("/ai-threads/{thread_id}")
def delete_thread(thread_id: int) -> dict:
    try:
        chat.delete_thread(thread_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": thread_id}


@router.get("/ai-threads/{thread_id}/messages")
def list_messages(thread_id: int) -> dict:
    try:
        return {"messages": chat.list_messages(thread_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/ai-threads/{thread_id}/messages")
async def send_message(thread_id: int, payload: MessageIn):
    settings = load_settings()
    try:
        thread = chat.get_thread(thread_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    client = chat.client_for(settings)
    if not client.configured:
        raise HTTPException(status_code=400, detail="未配置 AI 模型，请先在设置页填写")

    card_id = int(thread["kp_id"])
    _reserve(card_id)
    try:
        chat.append_message(thread_id, "user", payload.content.strip())
        messages = chat.build_messages(settings, thread_id)
    except Exception:
        _release(card_id)
        raise

    return StreamingResponse(
        _stream_reply(thread_id, card_id, client, messages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        background=BackgroundTask(_release, card_id),
    )


@router.post("/ai-threads/{thread_id}/messages/{message_id}/answer")
def set_current_answer(thread_id: int, message_id: int) -> dict:
    try:
        card_id = chat.set_answer(thread_id, message_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_card(card_id)


@router.delete("/cards/{card_id}/ai-answer")
def clear_answer(card_id: int) -> dict:
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT id FROM knowledge_point WHERE id = ?", (card_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="卡片不存在")
        conn.execute(
            "UPDATE knowledge_point SET ai_back_md = NULL, ai_status = 'none', "
            "ai_error = NULL, ai_generated_at = NULL, ai_answer_message_id = NULL "
            "WHERE id = ?",
            (card_id,),
        )
    return get_card(card_id)
