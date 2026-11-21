"""AI 答案建议稿：为草稿卡片生成面试口述版 Markdown 答案。

答案存储在 knowledge_point.ai_back_md，与 back_md、状态机和 Anki 推送完全隔离，
必须由人工在编辑页采用后才会进入正式背面。
"""

import asyncio
import threading
from contextlib import closing
from typing import Callable

from ..ai import AIClient, AIError
from ..db import connect, utc_now

Progress = Callable[[str, int, int], None]

MAX_VARIANTS = 5
MAX_CONTEXT_CHARS = 300
MAX_FRONT_CHARS = 500

_batch_lock = threading.Lock()

SYSTEM_PROMPT = """你是一名资深面试辅导老师，负责把面试题整理成能直接背诵、口述复述的答案。

要求：
1. 按「一句话核心结论 → 3-5 个要点 → 常见追问与易错点」的结构展开；必要时用表格或代码块辅助。
2. 按面试口述的节奏写：短句、分点、有因果，不要写成教科书章节。
3. 只写你有把握的内容。版本号、数据、源码细节无法确认时标注“（待核实）”，禁止编造。
4. 长度控制在 400-900 字，重点覆盖面试官最可能追问的点；宁可少写一个要点，也不要注水重复。
5. 用户会给出该题在真实面经中的问法变体与上下文，用来判断考察深度，但不要照抄原文。
6. 自我介绍、职业规划、项目难点等个人经历类问题，给出回答框架和示例要点，不要虚构经历。
7. 只输出 Markdown 正文，不要写“好的”“以下是答案”等前后说明，也不要用 ``` 包裹整段。"""


def is_running() -> bool:
    return _batch_lock.locked()


def _notify(progress: Progress | None, phase: str, done: int, total: int) -> None:
    if progress:
        progress(phase, done, total)


def _client(settings: dict) -> AIClient:
    """答案模型可单独配置，留空时回退到通用的 Chat 配置。"""
    return AIClient(
        settings.get("ai_answer_base_url") or settings.get("ai_base_url", ""),
        settings.get("ai_answer_api_key") or settings.get("ai_api_key", ""),
        settings.get("ai_answer_model") or settings.get("ai_chat_model", ""),
        reasoning_effort=settings.get("ai_answer_reasoning_effort") or "",
    )


def build_user_prompt(card: dict, variants: list[dict]) -> str:
    lines = [f"题目：{(card.get('front_md') or '')[:MAX_FRONT_CHARS]}"]
    if card.get("module_path"):
        lines.append(f"所属模块：{card['module_path']}")

    if variants:
        lines.append("真实面经中的问法（供参考）：")
        for item in variants[:MAX_VARIANTS]:
            section = item.get("section") or "未分环节"
            question = (item.get("raw_text") or "").strip()
            if not question:
                continue
            lines.append(f"- [{section}] {question}")
            context = (item.get("context_md") or "").strip()
            if context:
                lines.append(f"  上下文：{context[:MAX_CONTEXT_CHARS]}")
    return "\n".join(lines)


def _clean_answer(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        body = text.split("\n", 1)
        text = body[1] if len(body) > 1 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def generate_answer(client: AIClient, card: dict, variants: list[dict]) -> str:
    text = client.chat_text(SYSTEM_PROMPT, build_user_prompt(card, variants))
    answer = _clean_answer(text)
    if not answer:
        raise AIError("模型返回空答案")
    return answer


async def generate_answer_async(
    client: AIClient, card: dict, variants: list[dict]
) -> str:
    text = await client.chat_text_async(
        SYSTEM_PROMPT, build_user_prompt(card, variants)
    )
    answer = _clean_answer(text)
    if not answer:
        raise AIError("模型返回空答案")
    return answer


def _load_card(conn, card_id: int):
    row = conn.execute(
        """
        SELECT kp.*, m.path AS module_path
        FROM knowledge_point kp
        LEFT JOIN module m ON m.id = kp.module_id
        WHERE kp.id = ?
        """,
        (card_id,),
    ).fetchone()
    if row is None:
        return None, []
    variants = conn.execute(
        """
        SELECT raw_text, section, context_md
        FROM mention WHERE kp_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (card_id, MAX_VARIANTS * 2),
    ).fetchall()
    return row, [dict(item) for item in variants]


def _prepare_card(card_id: int, force: bool):
    """校验卡片并标记为生成中，返回 (card, variants)；需要跳过时返回 None。"""
    with closing(connect()) as conn, conn:
        card, variants = _load_card(conn, card_id)
        if card is None:
            raise LookupError("卡片不存在")
        if card["status"] != "draft" and not force:
            raise ValueError("只有草稿卡片可以生成 AI 答案")
        if (card["ai_back_md"] or "").strip() and not force:
            return None
        conn.execute(
            "UPDATE knowledge_point SET ai_status = 'running', ai_error = NULL "
            "WHERE id = ?",
            (card_id,),
        )
        return dict(card), variants


def _save_answer(card_id: int, answer: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            "UPDATE knowledge_point SET ai_back_md = ?, ai_status = 'done', "
            "ai_error = NULL, ai_generated_at = ? WHERE id = ?",
            (answer, utc_now(), card_id),
        )


def _mark_failed(card_id: int, message: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            "UPDATE knowledge_point SET ai_status = 'failed', ai_error = ? "
            "WHERE id = ?",
            (message[:500], card_id),
        )


def _reset_running(card_id: int) -> None:
    """取消后恢复状态：保留已有答案标记为 done，否则回到 none。"""
    with closing(connect()) as conn, conn:
        conn.execute(
            "UPDATE knowledge_point SET ai_status = "
            "CASE WHEN TRIM(COALESCE(ai_back_md, '')) = '' THEN 'none' ELSE 'done' END, "
            "ai_error = NULL WHERE id = ? AND ai_status = 'running'",
            (card_id,),
        )


def _generate_with_client(client: AIClient, card_id: int, force: bool) -> dict:
    prepared = _prepare_card(card_id, force)
    if prepared is None:
        return {"card_id": card_id, "skipped": True}
    card, variants = prepared
    try:
        answer = generate_answer(client, card, variants)
    except AIError as exc:
        _mark_failed(card_id, str(exc))
        raise
    _save_answer(card_id, answer)
    return {"card_id": card_id, "skipped": False}


async def _generate_with_client_async(
    client: AIClient, card_id: int, force: bool
) -> dict:
    prepared = _prepare_card(card_id, force)
    if prepared is None:
        return {"card_id": card_id, "skipped": True}
    card, variants = prepared
    try:
        answer = await generate_answer_async(client, card, variants)
    except asyncio.CancelledError:
        _reset_running(card_id)
        raise
    except AIError as exc:
        _mark_failed(card_id, str(exc))
        raise
    _save_answer(card_id, answer)
    return {"card_id": card_id, "skipped": False}


def generate_for_card(settings: dict, card_id: int, force: bool = False) -> dict:
    client = _client(settings)
    if not client.configured:
        raise AIError("未配置 AI 模型，请先在设置页填写")
    return _generate_with_client(client, card_id, force)


async def generate_for_card_async(
    settings: dict, card_id: int, force: bool = False
) -> dict:
    client = _client(settings)
    if not client.configured:
        raise AIError("未配置 AI 模型，请先在设置页填写")
    return await _generate_with_client_async(client, card_id, force)


def run_batch(
    settings: dict,
    card_ids: list[int],
    force: bool = False,
    progress: Progress | None = None,
) -> dict:
    if not _batch_lock.acquire(blocking=False):
        raise RuntimeError("已有 AI 答案任务在运行")
    try:
        client = _client(settings)
        if not client.configured:
            raise AIError("未配置 AI 模型，请先在设置页填写")

        report = {
            "total": len(card_ids),
            "done": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }
        for index, card_id in enumerate(card_ids, start=1):
            _notify(progress, "生成 AI 答案", index - 1, len(card_ids))
            try:
                result = _generate_with_client(client, card_id, force)
            except (AIError, LookupError, ValueError) as exc:
                report["failed"] += 1
                report["errors"].append(f"卡片 {card_id}: {exc}")
                continue
            if result["skipped"]:
                report["skipped"] += 1
            else:
                report["done"] += 1
        _notify(progress, "生成 AI 答案", len(card_ids), len(card_ids))
        return report
    finally:
        _batch_lock.release()
