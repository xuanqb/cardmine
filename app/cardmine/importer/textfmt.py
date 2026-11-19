"""Anki 字段与系统 Markdown 的互转、正面归一化。"""

import html
import re

_IMG_RE = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*>', re.I)
_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
_PRE_RE = re.compile(
    r"<pre[^>]*>\s*<code(?P<attrs>[^>]*)>(?P<body>.*?)</code>\s*</pre>",
    re.I | re.S,
)
_LANG_RE = re.compile(r'class="language-([^"]+)"', re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_BLANK_RE = re.compile(r"\n{3,}")
_TAG_RE = re.compile(r"<[^>]+>")
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_IMG_SRC_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)\s*\)")


def anki_to_markdown(text: str) -> str:
    """按需求 7.1 把 Anki 字段转换为纯 Markdown。"""
    if not text:
        return ""

    text = html.unescape(text)

    code_blocks: list[str] = []

    def stash_code(match: re.Match) -> str:
        lang_match = _LANG_RE.search(match.group("attrs"))
        lang = lang_match.group(1) if lang_match else ""
        body = _BR_RE.sub("\n", match.group("body"))
        code_blocks.append(f"```{lang}\n{body}\n```")
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    text = _PRE_RE.sub(stash_code, text)
    text = _BR_RE.sub("\n", text)
    text = _IMG_RE.sub(lambda m: f"![]({m.group(1)})", text)
    text = text.strip()
    text = _BLANK_RE.sub("\n\n", text)

    for index, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{index}\x00", block)
    return text


def markdown_to_anki(text: str, convert_newlines: bool = True) -> str:
    """按需求 7.2 把系统 Markdown 转为 Anki 字段（图片保持真实 img 标签）。"""
    if not text:
        return ""

    images: list[str] = []

    def stash_image(match: re.Match) -> str:
        images.append(match.group(1))
        return f"\x00IMG{len(images) - 1}\x00"

    text = _MD_IMG_SRC_RE.sub(stash_image, text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if convert_newlines:
        text = text.replace("\n", "<br>")

    for index, name in enumerate(images):
        safe = html.escape(name, quote=True)
        text = text.replace(f"\x00IMG{index}\x00", f'<img src="{safe}">')
    return text


def normalize_front(front_md: str) -> str:
    """正面归一化：去图片/标签/空白与句末标点，用于唯一键与接管匹配。"""
    text = _MD_IMG_RE.sub("", front_md)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", "", text)
    text = text.strip("?？。.！!；;，,、：:")
    return text.casefold()


def media_refs(*fields: str) -> set[str]:
    """提取字段中引用的媒体文件名（图片与音频）。"""
    refs: set[str] = set()
    for text in fields:
        if not text:
            continue
        refs.update(_IMG_RE.findall(text))
        refs.update(_SOUND_RE.findall(text))
    return refs
