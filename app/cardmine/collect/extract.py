"""AI 切题：从面经正文抽取问题提及。"""

import re

from ..ai import AIClient, AIError
from .clean import SECTION_RE

MAX_INPUT_CHARS = 16000
TOPICS = {"tech", "algo", "nontech"}
SUBTYPES = {"template", "personal"}

SYSTEM_PROMPT = """你是一名面经题库整理助手。用户会给出一篇面经正文与已有模块列表，请抽取其中所有真正的面试问题。

规则：
1. 只抽取面试官提出的问题或明确要求考察的点；过滤抒情、致谢、自述、公司评价等非提问内容。
2. 把口语化问题改写为规范、独立、可回答的问法，保留原意，不要编造原文没有的信息。
3. topic 取值：tech（技术知识点）、algo（算法题）、nontech（非技术题）。
4. nontech 必须给出 subtype：template（自我介绍、优缺点、职业规划、为什么选择我们等通用模板题）或 personal（介绍你的项目、项目难点等个性化经历题）；其它 topic 的 subtype 为 null。
5. section 为该问题所属环节（一面/二面/三面/HR面/部门/岗位/时长/算法环节），无法判断时为空字符串。
6. context 摘录面经中该问题附近的原句（最多 2 句），用于补答案时参考。
7. module 从给定模块列表中选择最合适的路径；确实没有合适模块时给出一个简短的新路径（最多 3 级，用 / 分隔）。
8. 同一篇面经中语义相同的问题只输出一次。

只输出 JSON，格式：
{"mentions":[{"question":"...","topic":"tech","subtype":null,"section":"一面","context":"...","module":"Hollis八股文/MySQL"}]}"""


def _sanitize_module(value) -> str:
    if not isinstance(value, str):
        return ""
    parts = [part.strip() for part in value.strip().strip("/").split("/") if part.strip()]
    return "/".join(parts[:3])


def _sanitize_section(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    match = SECTION_RE.match(value)
    if match:
        return "HR面" if match.group(1).lower().startswith("hr") else match.group(1)
    return value[:20]


def normalize_mentions(payload: dict) -> list[dict]:
    raw = payload.get("mentions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    mentions: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if len(question) < 2:
            continue
        key = re.sub(r"\s+", "", question)
        if key in seen:
            continue
        seen.add(key)

        topic = str(item.get("topic") or "").strip().lower()
        if topic not in TOPICS:
            topic = "tech"
        subtype = str(item.get("subtype") or "").strip().lower() or None
        if topic == "nontech":
            if subtype not in SUBTYPES:
                subtype = "template"
        else:
            subtype = None

        mentions.append(
            {
                "question": question[:500],
                "topic": topic,
                "subtype": subtype,
                "section": _sanitize_section(item.get("section")),
                "context": str(item.get("context") or "").strip()[:1000] or None,
                "module": _sanitize_module(item.get("module")),
            }
        )
    return mentions


def extract_mentions(
    client: AIClient, post: dict, module_paths: list[str]
) -> list[dict]:
    text = (post.get("clean_md") or "")[:MAX_INPUT_CHARS]
    if not text:
        return []

    user = (
        f"标题：{post.get('title') or ''}\n"
        f"作者：{post.get('author') or ''}\n"
        f"已有模块：\n" + "\n".join(f"- {path}" for path in module_paths[:80]) + "\n\n"
        f"面经正文：\n{text}"
    )
    payload = client.chat_json(SYSTEM_PROMPT, user)
    return normalize_mentions(payload)
