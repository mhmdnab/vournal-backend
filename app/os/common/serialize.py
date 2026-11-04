"""JSON exactly as the NestJS backend produced it, for /api only.

Prisma JS returned Decimal as a JSON number (vournalOS's DecimalInterceptor),
dates as `Date.toJSON()` (UTC, milliseconds, `Z`) and only the relations a query
included. prisma-client-py returns Decimal, possibly naive datetimes and pydantic
models with unloaded relations set to None, so everything /api returns goes
through to_json() first.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from prisma.bases import _PrismaModel
from pydantic import BaseModel

# Prisma field name → JSON key, for any field the Python client made us rename.
# Empty so far: every vournal OS field name generated as it is.
FIELD_ALIASES: dict[str, str] = {}


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)  # Prisma stores UTC
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _number(value: Decimal) -> int | float:
    # JS Number("12.00") is 12, so an integral amount is an int here too.
    return int(value) if value == value.to_integral_value() else float(value)


def to_json(value: Any) -> Any:
    if isinstance(value, float):
        # JS has one number type: JSON.stringify(12.0) is "12" (and -0 is "0"); from 1e21 it uses exponents
        return int(value) if value.is_integer() and abs(value) < 1e21 else value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return _number(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return _iso(datetime(value.year, value.month, value.day, tzinfo=timezone.utc))
    if isinstance(value, BaseModel):
        # A database row: unloaded relations were never set, and leaving them out
        # matches Prisma JS. Any other model is a whole answer, defaults included.
        names = value.model_fields_set if isinstance(value, _PrismaModel) else type(value).model_fields
        fields = {name: getattr(value, name) for name in names}
        return {FIELD_ALIASES.get(k, k): to_json(v) for k, v in fields.items()}
    if isinstance(value, dict):
        return {FIELD_ALIASES.get(k, k) if isinstance(k, str) else k: to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json(v) for v in value]
    raise TypeError(f"Can't serialise {type(value).__name__} for /api")
