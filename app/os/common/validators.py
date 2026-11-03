"""Field rules shared by vournal OS request bodies (vournalOS common/validators)."""

import math
import re

from app.os.common.jsparse import decimal_places

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}\Z")
ICON_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\Z")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d\Z", re.ASCII)


def length_message(field: str, text: str, low: int, high: int) -> str | None:
    """class-validator's Length(low, high) message for `text`, or None when it fits:
    it names only the bound that failed."""
    if len(text) < low:
        return f"{field} must be longer than or equal to {low} characters"
    if len(text) > high:
        return f"{field} must be shorter than or equal to {high} characters"
    return None


def trimmed(value, field: str, low: int, high: int):
    """Trim, then check the length (Trim() + Length(low, high))."""
    if value is None:
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if problem := length_message(field, value, low, high):
        raise ValueError(problem)
    return value


def color(value, field: str = "color"):
    if value is not None and not HEX_COLOR.match(value):
        raise ValueError(f"{field} must be a hex color like #6366f1")
    return value


def icon(value, field: str = "icon"):
    if value is not None and (len(value) > 50 or not ICON_NAME.match(value)):
        raise ValueError(f"{field} must be a lucide icon name in kebab-case")
    return value


def _decimal_places(value) -> float:
    """Decimals as class-validator counts them from Number#toString ("1.5e-7" → 4).
    Infinite where class-validator threw (1e-7), so every limit refuses it."""
    places = decimal_places(value)
    return math.inf if places is None else places


def money(value, field: str, minimum: float | None = None):
    """IsNumber({maxDecimalPlaces: 2}) (+ Min): a JSON number with at most 2 decimals."""
    if value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
        raise ValueError(f"{field} must be a number conforming to the specified constraints")
    if _decimal_places(value) > 2:
        raise ValueError(f"{field} must be a number conforming to the specified constraints")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must not be less than {minimum}")
    return value
