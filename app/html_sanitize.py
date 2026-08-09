"""Minimal allowlist HTML sanitizer for Academy rich-text lesson content."""

import re
from html import escape
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import urlparse, parse_qs


_ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "ul",
    "ol",
    "li",
    "h2",
    "h3",
    "h4",
    "a",
    "blockquote",
    "code",
    "pre",
    "span",
}
_VOID = {"br"}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out = []  # type: List[str]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            return
        if tag == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href" and v:
                    href = v.strip()
                    break
            if not href or not (
                href.startswith("http://")
                or href.startswith("https://")
                or href.startswith("mailto:")
            ):
                return
            self._out.append(f'<a href="{escape(href, quote=True)}" rel="noopener noreferrer" target="_blank">')
            return
        if tag in _VOID:
            self._out.append(f"<{tag}>")
            return
        self._out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _ALLOWED_TAGS and tag not in _VOID and tag != "a":
            self._out.append(f"</{tag}>")
        elif tag == "a":
            self._out.append("</a>")

    def handle_data(self, data):
        self._out.append(escape(data))

    def handle_entityref(self, name):
        self._out.append(f"&{name};")

    def handle_charref(self, name):
        self._out.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self._out)


def sanitize_rich_text(html):
    # type: (Optional[str]) -> str
    """Allow a small set of formatting tags; strip scripts/styles/events."""
    if not html:
        return ""
    text = html.strip()
    if not text:
        return ""
    # Legacy plain text (no tags) → escape + line breaks
    if "<" not in text:
        return "<p>" + escape(text).replace("\n", "<br>") + "</p>"
    parser = _Sanitizer()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return escape(text)
    return parser.result().strip()


def video_iframe_src(url):
    # type: (Optional[str]) -> Optional[str]
    """Normalize YouTube/Vimeo watch URLs to an embeddable iframe src."""
    if not url:
        return None
    raw = url.strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = parsed.path or ""

    if host in ("youtube.com", "m.youtube.com", "youtube-nocookie.com"):
        if path.startswith("/embed/") and len(path) > 7:
            return f"https://www.youtube-nocookie.com/embed/{path.split('/')[2]}"
        qs = parse_qs(parsed.query)
        vid = (qs.get("v") or [None])[0]
        if vid:
            return f"https://www.youtube-nocookie.com/embed/{vid}"
        m = re.match(r"^/shorts/([A-Za-z0-9_-]+)", path)
        if m:
            return f"https://www.youtube-nocookie.com/embed/{m.group(1)}"
    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
        if vid:
            return f"https://www.youtube-nocookie.com/embed/{vid}"
    if host in ("vimeo.com", "player.vimeo.com"):
        parts = [p for p in path.split("/") if p]
        if host == "player.vimeo.com" and parts and parts[0] == "video" and len(parts) > 1:
            return f"https://player.vimeo.com/video/{parts[1]}"
        if parts and parts[-1].isdigit():
            return f"https://player.vimeo.com/video/{parts[-1]}"
    # Direct https media / unknown provider — only allow https URLs for iframe
    if parsed.scheme == "https":
        return raw
    return None
