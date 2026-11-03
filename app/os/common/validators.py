"""Field rules shared by vournal OS request bodies (vournalOS common/validators)."""

import re

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
ICON_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def trimmed(value, field: str, low: int, high: int):
    """Trim, then check the length (Trim() + Length(low, high))."""
    if value is None:
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not low <= len(value) <= high:
        raise ValueError(
            f"{field} must be longer than or equal to {low} and shorter than or equal to {high} characters"
        )
    return value


def color(value, field: str = "color"):
    if value is not None and not HEX_COLOR.match(value):
        raise ValueError(f"{field} must be a hex color like #6366f1")
    return value


def icon(value, field: str = "icon"):
    if value is not None and (len(value) > 50 or not ICON_NAME.match(value)):
        raise ValueError(f"{field} must be a lucide icon name in kebab-case")
    return value
