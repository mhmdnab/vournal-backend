"""The one template every vournal email uses (designs approved by the owner 2026-11-24).

An email is a title plus a list of blocks. render() turns them into table-based HTML with
inline styles (what mail apps reliably show) and a plain-text twin with the links written
out, from the same words, so the two can't disagree. Light only, 560px wide.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

LOGO_URL = "https://www.vournal.app/apple-icon.png"
SITE = "https://www.vournal.app"
CONTACT = "hello@vournal.app"

PAPER, CARD, INK, SECOND, MUTED, LINE, WASH, ACCENT = (
    "#FAF8F3", "#FFFDF8", "#2B2926", "#6B6558", "#776F60", "#EAE3D6", "#EAE8DD", "#4A7556",
)
SERIF = "'Source Serif 4',Georgia,'Times New Roman',serif"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Helvetica,Arial,sans-serif"
MONO = "'SF Mono',Menlo,Consolas,monospace"

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def _inline(text: str) -> str:
    """Escaped HTML for a sentence; **words** become bold."""
    return _BOLD.sub(r"<strong>\1</strong>", esc(text))


def _plain(text: str) -> str:
    return _BOLD.sub(r"\1", text)


@dataclass
class P:
    text: str
    quiet: bool = False  # secondary colour, smaller
    gap: int = 14


@dataclass
class Code:
    code: str


@dataclass
class Button:
    label: str
    url: str


@dataclass
class Facts:
    rows: list[tuple[str, str]]


@dataclass
class Note:
    text: str


@dataclass
class LinkFallback:
    url: str


Block = P | Code | Button | Facts | Note | LinkFallback


def spaced_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}" if len(code) == 6 else code


def _html_block(block: Block) -> str:
    if isinstance(block, P):
        size, color = (14, SECOND) if block.quiet else (16, INK)
        return f'<p style="margin:{block.gap}px 0 0;font:{size}px/1.6 {SANS};color:{color}">{_inline(block.text)}</p>'
    if isinstance(block, Code):
        return (
            f'<div style="margin:24px 0 0;padding:18px 0;background:{WASH};border-radius:12px;text-align:center;'
            f'font:600 34px/1 {MONO};letter-spacing:6px;color:{INK}">{esc(spaced_code(block.code))}</div>'
        )
    if isinstance(block, Button):
        return (
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:28px 0 0"><tr>'
            f'<td style="border-radius:12px;background:{ACCENT}">'
            f'<a href="{esc(block.url)}" style="display:inline-block;padding:14px 26px;font:600 16px/1 {SANS};'
            f'color:#FFFFFF;text-decoration:none;border-radius:12px">{esc(block.label)}</a></td></tr></table>'
        )
    if isinstance(block, Facts):
        rows = "".join(
            f'<tr><td style="padding:10px 0;border-top:1px solid {LINE};font:14px/1.4 {SANS};color:{SECOND};width:42%">{esc(k)}</td>'
            f'<td style="padding:10px 0;border-top:1px solid {LINE};font:600 14px/1.4 {SANS};color:{INK}">{esc(v)}</td></tr>'
            for k, v in block.rows
        )
        return (
            f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
            f'style="margin:24px 0 0;border-bottom:1px solid {LINE}">{rows}</table>'
        )
    if isinstance(block, Note):
        return (
            f'<div style="margin:20px 0 0;padding:14px 16px;background:{WASH};border-radius:12px;'
            f'font:14px/1.55 {SANS};color:{INK}">{_inline(block.text)}</div>'
        )
    if isinstance(block, LinkFallback):
        return (
            f'<p style="margin:18px 0 0;font:13px/1.6 {SANS};color:{MUTED}">Or paste this link into your browser:<br>'
            f'<span style="color:{ACCENT};word-break:break-all">{esc(block.url)}</span></p>'
        )
    raise TypeError(block)


def _text_block(block: Block) -> str | None:
    if isinstance(block, (P, Note)):
        return _plain(block.text)
    if isinstance(block, Code):
        return block.code
    if isinstance(block, Button):
        return f"{block.label}: {block.url}"
    if isinstance(block, Facts):
        return "\n".join(f"{k}: {v}" for k, v in block.rows)
    if isinstance(block, LinkFallback):
        return None  # the button line already has the link
    raise TypeError(block)


def render(*, title: str, preheader: str, blocks: list[Block], reason: str, eyebrow: str | None = None) -> tuple[str, str]:
    """(html, text) for one email."""
    eb = (
        f'<p style="margin:0 0 10px;font:600 12px/1 {SANS};letter-spacing:1.2px;text-transform:uppercase;color:{ACCENT}">{esc(eyebrow)}</p>'
        if eyebrow else ""
    )
    body = "".join(_html_block(b) for b in blocks)
    html_doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>{esc(title)}</title></head>
<body style="margin:0;padding:0;background:{PAPER}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{esc(preheader)}</div>
<div style="background:{PAPER};padding:32px 16px">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:560px;margin:0 auto">
<tr><td style="padding:0 4px 20px"><table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td><img src="{LOGO_URL}" width="36" height="36" alt="vournal" style="display:block;border-radius:9px;border:1px solid {LINE}"></td>
<td style="padding-left:10px;font:500 21px/1 {SERIF};color:{INK}">vournal</td></tr></table></td></tr>
<tr><td style="background:{CARD};border:1px solid {LINE};border-radius:18px;padding:34px 30px">
{eb}<h1 style="margin:0;font:400 27px/1.25 {SERIF};color:{INK};letter-spacing:-0.2px">{esc(title)}</h1>
{body}
</td></tr>
<tr><td style="padding:22px 6px 0;font:13px/1.6 {SANS};color:{MUTED}">
{esc(reason)}<br>
vournal · <a href="mailto:{CONTACT}" style="color:{SECOND}">{CONTACT}</a> · Tripoli, Lebanon<br>
<a href="{SITE}/privacy" style="color:{SECOND}">Privacy</a> · <a href="{SITE}/terms" style="color:{SECOND}">Terms</a>
</td></tr></table></div></body></html>"""
    parts = [title, *(t for t in (_text_block(b) for b in blocks) if t)]
    text = "\n\n".join(parts) + f"\n\n--\n{reason}\nvournal · {CONTACT} · Tripoli, Lebanon"
    return html_doc, text


ACCOUNT_REASON = "You’re getting this because there’s a vournal account for this email address."
