"""把公开网页收成标题和正文。不执行页面里的脚本。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
import httpx

try:
    from .images import (
        CANDIDATES_PER_PAGE,
        FetchedImage,
        FetchPolicy,
        download_candidate_images,
        download_public,
        image_urls_in_document,
        is_html,
        make_policy,
        sniff_image,
        decode_image,
    )
    from .texts import MAX_CAPTURE_CHARS
except ImportError:
    from images import (
        CANDIDATES_PER_PAGE,
        FetchedImage,
        FetchPolicy,
        download_candidate_images,
        download_public,
        image_urls_in_document,
        is_html,
        make_policy,
        sniff_image,
        decode_image,
    )
    from texts import MAX_CAPTURE_CHARS


@dataclass(frozen=True)
class PageRead:
    """一次打开网页的结果。图片已经核对过格式，正文是纯文本。"""

    final_url: str
    title: str
    text: str
    images: list[FetchedImage]
    notes: list[str]
    candidates: list[str] = field(default_factory=list)
    candidates_limited: bool = False
    captured_text: str = ''
    capture_limited: bool = False
    image_url: bool = False


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self.description = ""
        self._parts: list[str] = []
        self._main_parts: list[str] = []
        self._article_parts: list[str] = []
        self._main_depth = 0
        self._article_depth = 0
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        if name in {"script", "style", "noscript", "svg", "template", "nav", "header", "footer", "aside", "form"}:
            self._skip += 1
            return
        if self._skip:
            return
        if name == 'main':
            self._main_depth += 1
        if name == 'article':
            self._article_depth += 1
        if name == "title":
            self._in_title = True
        values = {key.lower(): value or "" for key, value in attrs}
        meta = (values.get("property") or values.get("name") or "").lower()
        if name == "meta" and meta in {"description", "og:description"} and not self.description:
            self.description = values.get("content") or ""
        if name == "meta" and meta == "og:title" and not self.og_title:
            self.og_title = values.get("content") or ""
        if name in {"p", "br", "div", "h1", "h2", "h3", "li", "tr", "section", "article"}:
            self.add_part('\n')

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"script", "style", "noscript", "svg", "template", "nav", "header", "footer", "aside", "form"} and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if name == 'main':
            self._main_depth = max(0, self._main_depth - 1)
        if name == 'article':
            self._article_depth = max(0, self._article_depth - 1)
        if name == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        text = " ".join(data.split())
        if text:
            self.add_part(text)

    def add_part(self, text):
        self._parts.append(text)
        if self._main_depth:
            self._main_parts.append(text)
        if self._article_depth:
            self._article_parts.append(text)


def _collapse(parts: list[str], limit: int) -> str:
    lines: list[str] = []
    for part in parts:
        if part == "\n":
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(part)
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…（正文已截断）"


def page_text(data: bytes, content_type: str, limit: int) -> tuple[str, str]:
    """从 HTML、JSON 或纯文本里取出标题和正文。"""

    declared = content_type.split(";", 1)[0].strip().lower()
    charset = "utf-8"
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip(" \"'")
    try:
        text = data.decode(charset, "replace")
    except LookupError:
        text = data.decode("utf-8", "replace")
    if declared == "application/json" or text.lstrip().startswith(("{", "[")):
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, RecursionError):
            pass
        return "", _collapse([text], limit)
    if not is_html(data, content_type) and declared.startswith("text/"):
        return "", _collapse(text.splitlines(), limit)
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    title = " ".join((parser.title or parser.og_title).split())
    description = " ".join(parser.description.split())
    preferred = parser._article_parts or parser._main_parts or parser._parts
    body = _collapse(preferred, limit)
    if description and not (parser._article_parts or parser._main_parts) and description not in body[:400]:
        body = _collapse([description, "\n", body], limit)
    return title[:200], body


async def read_public_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    policy: Optional[FetchPolicy] = None,
    resolver=None,
    blocked_ips=None,
    allow_unresolved: bool = False,
    check_peer: bool = True,
    max_images: int = 4,
    max_image_bytes: int = 8 * 1024 * 1024,
    text_limit: int = 8000,
    preview_only: bool = False,
    capture_text: bool = False,
    include_images: bool = True,
) -> PageRead:
    """打开一个公开地址，读取正文并下载其中的图片。"""

    active = policy or make_policy(
        resolver=resolver,
        blocked_ips=blocked_ips,
        allow_unresolved=allow_unresolved,
        check_peer=check_peer,
        max_images=max_images,
        max_image_bytes=max_image_bytes,
    )
    data, content_type, final = await download_public(client, url, policy=active)
    mime = sniff_image(data)
    if mime:
        if not include_images:
            return PageRead(final, '', '', [], [], image_url=True)
        return PageRead(final, "", "", [await decode_image(data, final)], [])
    title, text = page_text(data, content_type, MAX_CAPTURE_CHARS + 1 if capture_text else text_limit)
    captured = text[:MAX_CAPTURE_CHARS] if capture_text else ''
    capture_limited = capture_text and len(text) > MAX_CAPTURE_CHARS
    if capture_text and len(text) > text_limit:
        text = text[:text_limit] + '\n…（正文已截断）'
    if not include_images:
        return PageRead(final, title, text, [], [], captured_text=captured, capture_limited=capture_limited)
    images: list[FetchedImage] = []
    notes: list[str] = []
    candidates: list[str] = []
    limited = False
    if is_html(data, content_type):
        document = data.decode("utf-8", "replace")
        candidates = image_urls_in_document(document, final, CANDIDATES_PER_PAGE + 1)
        limited = len(candidates) >= CANDIDATES_PER_PAGE
        candidates = candidates[:CANDIDATES_PER_PAGE]
        if not preview_only:
            await download_candidate_images(client, candidates, active, images, notes)
    return PageRead(final, title, text, images, notes, candidates, limited, captured, capture_limited)
