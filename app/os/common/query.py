"""Query-string parsing as vournalOS's DTO transforms and ValidationPipe did it. Each
reader appends class-validator's message to `errors` and returns None on a bad value."""

import math

from fastapi import Request

from app.os.common import dates
from app.os.common.jsparse import js_number
from app.os.common.validators import int_problems


def string_list(value) -> list[str] | None:
    """?types=task,note or ?types=task&types=note → ["task", "note"] (ToStringArray)."""
    if value is None or value == "" or value == []:
        return None
    parts = value if isinstance(value, list) else [value]
    out = [piece.strip() for part in parts for piece in str(part).split(",")]
    return [p for p in out if p] or None


def unknown_params(request: Request, allowed: tuple[str, ...]) -> list[str]:
    """forbidNonWhitelisted on a @Query() DTO: a key the DTO doesn't declare is a 400.
    These messages come first, as class-validator's whitelist check runs first."""
    return [f"property {key} should not exist" for key in dict.fromkeys(request.query_params.keys()) if key not in allowed]


def _query_bool(request: Request, name: str, errors: list) -> bool | None:
    value = request.query_params.get(name)
    if value is None or value == "":
        return None
    v = value.lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    errors.append(f"{name} must be a boolean value")
    return None


def _query_int(request: Request, name: str, errors: list, maximum: int | None = None, minimum: int = 1) -> int | None:
    """@Type(() => Number) @IsInt() @Min(minimum) [@Max(maximum)]: every failing rule is
    reported, in class-validator's order (the last decorator first)."""
    value = request.query_params.get(name)
    if value is None:
        return None
    number = js_number(value)
    problems = int_problems(number, name, minimum, maximum)
    errors.extend(problems)
    return None if problems else int(number)


def _query_list(request: Request, name: str, allowed, errors: list) -> list[str] | None:
    values = string_list(request.query_params.getlist(name))
    if values and any(v not in allowed for v in values):
        errors.append(f"each value in {name} must be one of the following values: {', '.join(allowed)}")
    return values


def _query_date(request: Request, name: str, errors: list) -> str | None:
    value = request.query_params.get(name)
    if value is not None and not dates.is_iso_date_string(value):
        errors.append(f"{name} must be a valid ISO 8601 date string")
    return value
