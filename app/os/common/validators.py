"""Field rules shared by vournal OS request bodies (vournalOS common/validators)."""

import math
import re

from app.os.common.jsparse import _JS_SPACE, decimal_places

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}\Z")
ICON_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\Z")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d\Z", re.ASCII)


def int_problems(number: float, field: str, minimum: float | None = None, maximum: float | None = None) -> list[str]:
    """@IsInt() @Min(minimum) @Max(maximum) on a number: every failing rule, in
    class-validator's order (the last decorator first). NaN fails all of them."""
    problems = []
    if maximum is not None and not number <= maximum:
        problems.append(f"{field} must not be greater than {maximum}")
    if minimum is not None and not number >= minimum:
        problems.append(f"{field} must not be less than {minimum}")
    if not (math.isfinite(number) and number == int(number)):
        problems.append(f"{field} must be an integer number")
    return problems


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
    value = value.strip(_JS_SPACE)  # String#trim's set: a byte-order mark is space
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


# ----- class-validator rules on any JSON value -------------------------------------
# These take whatever the body held (a wrong type included) and return every failing
# rule's message in class-validator's order: the last decorator first.

_NUMBER_WORDS = "must be a number conforming to the specified constraints"


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def number_rules(value, field: str, minimum=None, maximum=None, places: int | None = None) -> list[str]:
    """@IsNumber({maxDecimalPlaces: places}) — or @IsInt() when places is None — then
    @Min(minimum) @Max(maximum). A non-number fails every rule."""
    number = value if _is_number(value) else math.nan
    if places is None:
        return int_problems(number, field, minimum, maximum)
    problems = []
    if maximum is not None and not number <= maximum:
        problems.append(f"{field} must not be greater than {maximum}")
    if minimum is not None and not number >= minimum:
        problems.append(f"{field} must not be less than {minimum}")
    if not (math.isfinite(number) and _decimal_places(number) <= places):
        problems.append(f"{field} {_NUMBER_WORDS}")
    return problems


def _js_truthy(value) -> bool:
    return value not in (None, False, "", 0) and value == value


def text_rules(value, field: str, maximum: int, minimum: int | None = None) -> list[str]:
    """@IsString() then @MaxLength(maximum), or @Length(minimum, maximum) when a minimum
    is given (after Trim). Length's message for a non-string follows its JavaScript
    default: "longer" when the value is falsy, "shorter" past the limit, else both."""
    if isinstance(value, str):
        problem = length_message(field, value, minimum or 0, maximum)
        return [problem] if problem else []
    if minimum is None:
        return [f"{field} must be shorter than or equal to {maximum} characters", f"{field} must be a string"]
    size = len(value) if isinstance(value, list) else None
    if not _js_truthy(value) or (size is not None and size < minimum):
        problem = f"{field} must be longer than or equal to {minimum} characters"
    elif size is not None and size > maximum:
        problem = f"{field} must be shorter than or equal to {maximum} characters"
    else:
        problem = f"{field} must be longer than or equal to {minimum} and shorter than or equal to {maximum} characters"
    return [problem, f"{field} must be a string"]


def choice_rules(value, field: str, choices: tuple[str, ...]) -> list[str]:
    """@IsEnum / @IsIn."""
    return [] if isinstance(value, str) and value in choices else [f"{field} must be one of the following values: {', '.join(choices)}"]


def bool_rules(value, field: str) -> list[str]:
    return [] if isinstance(value, bool) else [f"{field} must be a boolean value"]


def string_rules(value, field: str) -> list[str]:
    return [] if isinstance(value, str) else [f"{field} must be a string"]


def color_rules(value, field: str) -> list[str]:
    """@IsColor(): Matches(#rrggbb)."""
    return [] if isinstance(value, str) and HEX_COLOR.match(value) else [f"{field} must be a hex color like #6366f1"]


# validator.js isEmail with its defaults (UTF-8 local part, a domain with a TLD, no IP).
_UTF8_LETTERS = r"¡-퟿豈-﷏ﷰ-￯"
_EMAIL_LOCAL = re.compile(rf"^[a-z\d!#$%&'*+\-/=?^_`{{|}}~{_UTF8_LETTERS}]+\Z", re.I)
_QUOTED_LOCAL = re.compile(
    rf"^(?:[\s\x01-\x08\x0b\x0c\x0e-\x1f\x7f\x21\x23-\x5b\x5d-\x7e{_UTF8_LETTERS}]|\\[\x01-\x09\x0b\x0c\x0d-\x7f{_UTF8_LETTERS}])*\Z",
    re.I,
)
_TLD = re.compile(r"^(?:[a-z¡-¨ª-퟿豈-﷏ﷰ-￯]{2,}|xn[a-z0-9-]{2,})\Z", re.I)
# JavaScript's ¡-￿ takes both halves of a surrogate pair, so astral characters pass
_LABEL = re.compile(r"^[a-z_¡-￿\U00010000-\U0010ffff0-9-]+\Z", re.I)
_FULL_WIDTH = re.compile(r"[！-～]")


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def is_fqdn(domain: str) -> bool:
    """validator.js isFQDN with require_tld (and no trailing dot, numeric TLD or underscore)."""
    parts = domain.split(".")
    tld = parts[-1]
    if len(parts) < 2 or not _TLD.match(tld) or re.search(r"\s", tld) or re.fullmatch(r"\d+", tld):
        return False
    for part in parts:
        if _utf16_length(part) > 63 or not _LABEL.match(part) or _FULL_WIDTH.search(part):
            return False
        if part.startswith("-") or part.endswith("-") or "_" in part:
            return False
    return True


def is_email(text: str) -> bool:
    if _utf16_length(text) > 254 or "@" not in text:
        return False
    user, _, domain = text.rpartition("@")
    domain = domain.lower()
    if len(user.encode()) > 64 or len(domain.encode()) > 254 or not is_fqdn(domain):
        return False
    if user.startswith('"') and user.endswith('"'):
        return bool(_QUOTED_LOCAL.match(user[1:-1]))
    return all(_EMAIL_LOCAL.match(part) for part in user.split("."))


def email_rules(value, field: str, maximum: int) -> list[str]:
    """@IsEmail() then @MaxLength(maximum): MaxLength's message first."""
    if not isinstance(value, str):
        return [f"{field} must be shorter than or equal to {maximum} characters", f"{field} must be an email"]
    problems = [f"{field} must be shorter than or equal to {maximum} characters"] if _utf16_length(value) > maximum else []
    return problems + ([] if is_email(value) else [f"{field} must be an email"])


_HTTP = re.compile(r"^https?://", re.I)


def url_rules(value, field: str, maximum: int) -> list[str]:
    """@IsUrl({ require_protocol: true, protocols: ['http', 'https'] }) then @MaxLength."""
    from app.os.recurring_payments import _URL  # validator.js isURL's shape

    if not isinstance(value, str):
        return [f"{field} must be shorter than or equal to {maximum} characters", f"{field} must be a URL address"]
    problems = [f"{field} must be shorter than or equal to {maximum} characters"] if _utf16_length(value) > maximum else []
    return problems + ([] if _HTTP.match(value) and _URL.match(value) else [f"{field} must be a URL address"])
