"""RSS 抓取与解析（RSS 2.0）。"""

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

USER_AGENT = "cardmine/0.1 (+feed reader)"


class FeedError(Exception):
    pass


@dataclass
class FeedItem:
    guid: str
    title: str
    description: str
    link: str | None
    author: str | None


def _text(node: ET.Element, name: str) -> str | None:
    child = node.find(name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def fetch_feed(url: str, timeout: int = 30) -> list[FeedItem]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise FeedError(f"无法访问 RSS: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FeedError("RSS 请求超时") from exc

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FeedError(f"RSS 解析失败: {exc}") from exc

    items: list[FeedItem] = []
    for node in root.findall(".//item"):
        link = _text(node, "link")
        guid = _text(node, "guid") or link
        title = _text(node, "title") or "（无标题）"
        if not guid:
            continue
        items.append(
            FeedItem(
                guid=guid,
                title=title,
                description=_text(node, "description") or "",
                link=link,
                author=_text(node, "author"),
            )
        )
    return items
