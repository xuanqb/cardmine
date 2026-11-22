"""AI 对话的提示词与模型配置。"""

from ..ai import AIClient

MAX_VARIANTS = 5
MAX_CONTEXT_CHARS = 300
MAX_FRONT_CHARS = 500
MAX_PROFILE_NOTES = 3000

SYSTEM_PROMPT = """你是一名资深面试辅导老师，负责把面试题整理成能直接背诵、口述复述的答案。

要求：
1. 按「一句话核心结论 → 3-5 个要点 → 常见追问与易错点」的结构展开；必要时用表格或代码块辅助。
2. 按面试口述的节奏写：短句、分点、有因果，不要写成教科书章节。
3. 只写你有把握的内容。版本号、数据、源码细节无法确认时标注“（待核实）”，禁止编造。
4. 长度控制在 400-900 字，重点覆盖面试官最可能追问的点；宁可少写一个要点，也不要注水重复。
5. 用户会给出该题在真实面经中的问法变体与上下文，用来判断考察深度，但不要照抄原文。
6. 自我介绍、职业规划、项目难点等个人经历类问题，优先使用候选人背景中的真实信息；背景未提供或未覆盖的细节标注“（待补充）”，禁止虚构经历。
7. 只输出 Markdown 正文，不要写“好的”“以下是答案”等前后说明，也不要用 ``` 包裹整段。"""

PROFILE_RULES = """使用规则：
- 只使用上述背景中的信息，不要补写背景里没有的公司、项目、数字或经历。
- 背景未覆盖的细节统一写“（待补充）”，方便用户替换。
- 不主动提及外包、派遣、驻场等雇佣形式，也不要声称自己是正式编制；聚焦岗位职责和工作内容。
- 表述保持中性积极：强调交付能力、适应不同业务和业务理解，不贬低过往经历。"""


def _client(settings: dict) -> AIClient:
    """答案模型可单独配置，留空时回退到通用的 Chat 配置。"""
    return AIClient(
        settings.get("ai_answer_base_url") or settings.get("ai_base_url", ""),
        settings.get("ai_answer_api_key") or settings.get("ai_api_key", ""),
        settings.get("ai_answer_model") or settings.get("ai_chat_model", ""),
        reasoning_effort=settings.get("ai_answer_reasoning_effort") or "",
    )


def profile_from_settings(settings: dict) -> dict:
    return {
        "current_role": (settings.get("candidate_current_role") or "").strip(),
        "target_role": (settings.get("candidate_target_role") or "").strip(),
        "years": int(settings.get("candidate_years") or 0),
        "notes": (settings.get("candidate_notes") or "").strip()[:MAX_PROFILE_NOTES],
    }


def build_system_prompt(profile: dict | None = None) -> str:
    if not profile:
        return SYSTEM_PROMPT

    lines = []
    if profile.get("current_role"):
        lines.append(f"- 当前岗位：{profile['current_role']}")
    if profile.get("target_role"):
        lines.append(f"- 目标岗位：{profile['target_role']}")
    if profile.get("years"):
        lines.append(f"- 工作年限：{profile['years']} 年")
    notes = (profile.get("notes") or "").strip()
    if notes:
        lines.append(f"- 补充说明：\n{notes}")
    if not lines:
        return SYSTEM_PROMPT

    return (
        SYSTEM_PROMPT
        + "\n\n候选人背景（仅用于个人经历类问题；与当前题目无关时忽略）：\n"
        + "\n".join(lines)
        + "\n\n"
        + PROFILE_RULES
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
