"""Rich notes (design 2026-11-18): the server's side of the Word-style editor.

sanitize() is the only way HTML reaches the database: an allow-list of elements and attributes,
links limited to http(s)/mailto, images limited to our own attachment URLs, and styles limited to
colour, highlight, font, size and alignment with checked values (a paste from Word or Google Docs
keeps its colours; its fonts map to the editor's four, or go). html_text() and markdown_text() give
a note's words for lists, search and the AI, which never see tags."""

import re
from html import escape, unescape
from html.parser import HTMLParser

import nh3

from app.os.common.jsparse import _JS_SPACE

FONT_FAMILIES: dict[str, str] = {
    "sans": "Inter, system-ui, sans-serif",
    "serif": "Georgia, 'Times New Roman', serif",
    "mono": "ui-monospace, Menlo, monospace",
    "hand": "'Caveat', 'Comic Sans MS', cursive",
}
FONT_SIZES = (12, 14, 15, 16, 18, 20, 24, 28, 32)

TAGS = {"p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "u", "s", "mark", "span", "a", "ul", "ol", "li", "blockquote",
        "pre", "code", "table", "thead", "tbody", "tr", "th", "td", "colgroup", "col", "img", "hr"}
ATTRIBUTES = {
    "*": {"dir", "style"},
    "a": {"href", "target"},
    "img": {"src", "width"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
    "ul": {"data-type"},
    "li": {"data-type", "data-checked"},
}

# \Z, not $: a value with a trailing newline is not the same value
_IMG_SRC = re.compile(r"^/api/attachments/[A-Za-z0-9_-]+/download(\?space=[A-Za-z0-9_-]+)?\Z")
_COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\(\s*\d{1,3},\s*\d{1,3},\s*\d{1,3}(,\s*(0|1|0?\.\d+))?\s*\))\Z")
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)(px|pt)\Z")
_SPAN_NUM = re.compile(r"^[1-9]\d?\Z")
# documents (Google Docs, Word) stamp black-on-white on everything: unreadable in dark mode, so dropped
_DEFAULT_COLOR = re.compile(r"^(#000|#000000|rgba?\(\s*0,\s*0,\s*0(,\s*1(\.0+)?)?\s*\)|black)$", re.I)
_DEFAULT_BACKGROUND = re.compile(r"^(#fff|#ffffff|rgba?\(\s*255,\s*255,\s*255(,\s*1(\.0+)?)?\s*\)|white|transparent)$", re.I)
_ALIGN = {"left", "right", "center", "justify", "start", "end"}
_FAMILY_MAP = {
    "arial": "sans", "helvetica": "sans", "calibri": "sans", "inter": "sans", "system-ui": "sans", "sans-serif": "sans",
    "times": "serif", "times new roman": "serif", "georgia": "serif", "cambria": "serif", "serif": "serif",
    "courier": "mono", "courier new": "mono", "consolas": "mono", "ui-monospace": "mono", "monospace": "mono", "menlo": "mono",
    "caveat": "hand", "comic sans ms": "hand", "cursive": "hand",
}
_STACKS = {v: k for k, v in FONT_FAMILIES.items()}


def _font_family(value: str) -> str | None:
    if value in _STACKS:
        return value
    first = value.split(",")[0].strip().strip("'\"").lower()
    key = _FAMILY_MAP.get(first)
    return FONT_FAMILIES[key] if key else None


def _font_size(value: str) -> str | None:
    m = _SIZE.match(value)
    if not m:
        return None
    px = float(m[1]) * (4 / 3 if m[2] == "pt" else 1)
    return f"{min(FONT_SIZES, key=lambda s: abs(s - px))}px"


def _style(value: str) -> str | None:
    kept = []
    for decl in value.split(";"):
        if ":" not in decl:
            continue
        prop, val = (part.strip() for part in decl.split(":", 1))
        prop = prop.lower()
        if prop == "color" and _COLOR.match(val) and not _DEFAULT_COLOR.match(val):
            out = val
        elif prop == "background-color" and _COLOR.match(val) and not _DEFAULT_BACKGROUND.match(val):
            out = val
        elif prop == "font-family":
            out = _font_family(val)
        elif prop == "font-size":
            out = _font_size(val.lower())
        elif prop == "text-align" and val.lower() in _ALIGN:
            out = val.lower()
        else:
            out = None
        if out:
            kept.append(f"{prop}:{out}")
    return ";".join(kept) or None


def _filter(element: str, attribute: str, value: str) -> str | None:
    if attribute == "style":
        return _style(value)
    if attribute == "href":
        v = value.lower()
        if v.startswith(("http://", "https://", "mailto:", "tel:")):
            return value
        return value if v.startswith("/") and not v.startswith(("//", "/\\")) else None  # a page of this site
    if attribute == "target":
        return value if value == "_blank" else None
    if attribute == "src":
        return value if _IMG_SRC.match(value) else None
    if attribute == "width":
        return value if value in ("25%", "50%", "100%") else None
    if attribute in ("colspan", "rowspan"):
        return value if _SPAN_NUM.match(value) else None
    if attribute == "data-type":
        return value if value in ("taskList", "taskItem") else None
    if attribute == "data-checked":
        return value if value in ("true", "false") else None
    if attribute == "dir":
        return value if value in ("auto", "ltr", "rtl") else None
    return value


_SRC_LESS_IMG = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>")
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)


class _Attrs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.attrs: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        self.attrs = {k: v or "" for k, v in attrs}

    handle_startendtag = handle_starttag


def _image_or_link(match: re.Match) -> str:
    """Our own images stay; a web image (pasted from a page, or ![alt](url) in an old markdown note)
    becomes a link to it, so its address and words aren't lost (the site shows only our images)."""
    parser = _Attrs()
    parser.feed(match[0])
    src = parser.attrs.get("src", "").strip()
    if _IMG_SRC.match(src) or not src.lower().startswith(("http://", "https://")):
        return match[0]
    return f'<a href="{escape(src)}" target="_blank">{escape(parser.attrs.get("alt", "").strip() or src)}</a>'


def sanitize(html: str) -> str:
    html = _IMG_TAG.sub(_image_or_link, html)
    out = nh3.clean(html, tags=TAGS, clean_content_tags={"script", "style", "template", "iframe", "object"},
                    attributes=ATTRIBUTES, attribute_filter=_filter, url_schemes={"http", "https", "mailto", "tel"},
                    link_rel="noopener noreferrer", strip_comments=True)
    return _SRC_LESS_IMG.sub("", out)


_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "tr", "table", "ul", "ol", "hr", "div"}


class _Text(HTMLParser):
    """Text with one newline between blocks; an empty paragraph is a blank line; cells are tabbed."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.cells = 0
        self.blocks: list[bool] = []  # per open block: has it any text yet
        self.in_cell = 0  # the editor writes <td><p>…</p></td>: a cell's paragraph stays on the row

    def _newline(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.in_cell += 1
        if tag == "br":
            self.parts.append("\n")
        elif tag == "p" and self.in_cell:
            pass
        elif tag in _BLOCKS:
            self._newline()
            self.blocks.append(False)
        if tag == "tr":
            self.cells = 0
        if tag in ("td", "th"):
            if self.cells:
                self.parts.append("\t")
            self.cells += 1

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.in_cell = max(0, self.in_cell - 1)
        if tag == "p" and self.in_cell:
            return
        if tag in _BLOCKS and self.blocks:
            had_text = self.blocks.pop()
            if had_text:
                self._newline()
                if self.blocks:
                    self.blocks[-1] = True
            elif tag == "p":
                self.parts.append("\n")  # an empty paragraph: a blank line

    def handle_data(self, data):
        if data.strip():
            if self.blocks:
                self.blocks[-1] = True
            self.parts.append(data)
        elif self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append(data)


_INLINE_SPACE = re.compile(r"[ \f\v ​]+")


def html_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = []
    for line in "".join(parser.parts).split("\n"):
        cells = [_INLINE_SPACE.sub(" ", c).strip() for c in line.split("\t")]
        lines.append("\t".join(cells).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# markdown → words: the excerpt's steps (notes.py) without collapsing lines
_S = "[" + re.escape(_JS_SPACE) + "]"
_LINE_START = r"(?:^|(?<=[\r  ]))"
MARKDOWN_STEPS = (
    (re.compile(r"```[\s\S]*?```"), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(_LINE_START + _S + r"{0,3}(#{1,6}|>|[-*+]|[0-9]+\.)" + _S + "+", re.M), ""),
    (re.compile(r"\[( |x|X)\]" + _S + "*"), ""),
    (re.compile(r"(\*\*|__|~~|\*|_)"), ""),
)


# the same, with list and heading marks matched within a line, so blank lines stay
_TEXT_STEPS = MARKDOWN_STEPS[:4] + ((re.compile(r"^[ \t]{0,3}(#{1,6}|>|[-*+]|[0-9]+\.)[ \t]+", re.M), ""),) + MARKDOWN_STEPS[5:]


def markdown_text(md: str) -> str:
    text = md
    for pattern, replacement in _TEXT_STEPS:
        text = pattern.sub(replacement, text)
    lines = [_INLINE_SPACE.sub(" ", line).strip() for line in unescape(text).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
