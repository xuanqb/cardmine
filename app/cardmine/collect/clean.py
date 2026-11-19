"""面经 HTML 清洗：去 emoji/噪音标签，保留段落顺序。"""

import re
from html.parser import HTMLParser

BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "table",
    "tr",
    "blockquote",
}
SKIP_TAGS = {"script", "style"}
SECTION_RE = re.compile(
    r"^(?:#+\s*)?(一面|二面|三面|四面|五面|终面|HR面|hr面|部门|岗位|时长|算法环节|笔试)"
    r"\s*[:：]?\s*(.*)$"
)
_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_BLANK_RE = re.compile(r"\n{3,}")


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if tag == "img":
            # emoji 与正文图片均不进入文本
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip == 0:
            self.parts.append(data)


def html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    parser = _Extractor()
    parser.feed(raw_html)
    parser.close()

    text = "".join(parser.parts)
    lines = []
    for line in text.splitlines():
        line = _SPACE_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


def split_sections(text: str) -> list[tuple[str, str]]:
    """按章节标记切分，返回 (section, paragraph) 列表。"""
    current = ""
    result: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = SECTION_RE.match(stripped)
        if match:
            current = match.group(1)
            if current.startswith("hr"):
                current = "HR面"
            rest = match.group(2).strip()
            if rest:
                result.append((current, rest))
            continue
        result.append((current, stripped))
    return result


def clean_post(raw_html: str) -> str:
    return html_to_text(raw_html)
