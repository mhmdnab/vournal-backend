"""/api/documents (port of vournalOS DocumentsService). The number and notes are stored
encrypted with the app's field key (app/mfa.py; NestJS used its own AES key and there
is no vournalOS data to carry over), lists mask the number and leave the notes out."""

from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from app import mfa
from app.db import db
from app.os import attachments
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.jsparse import _JS_SPACE
from app.os.common.query import _query_list, unknown_params
from app.os.profile import ensure_os_profile

router = nest_router("/documents")

DEFAULT_EXPIRY_WINDOW_DAYS = 30
TYPES = (
    "PASSPORT", "NATIONAL_ID", "DRIVERS_LICENSE", "INSURANCE", "CONTRACT", "WARRANTY", "CERTIFICATE", "MEDICAL",
    "VEHICLE", "FINANCIAL", "OTHER",
)
STATUSES = ("valid", "expiringSoon", "expired", "noExpiry")
TypeName = Literal[TYPES]  # type: ignore[valid-type]


def _encrypt(value: str | None) -> str | None:
    return None if value is None else mfa.encrypt(value)


def _try_decrypt(value: str | None) -> str | None:
    """None for a missing or unreadable value: one bad row never breaks a list."""
    if value is None:
        return None
    try:
        return mfa.decrypt(value)
    except (InvalidToken, ValueError, TypeError):
        return None


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


def mask_number(value: str) -> str:
    """"P12345678" → "••••5678"; short values reveal less (4 or fewer: fully masked)."""
    clean = value.strip(_JS_SPACE)
    units = len(_utf16(clean)) // 2
    if units <= 4:
        return "••••"
    keep = 2 if units < 8 else 4
    return "••••" + _utf16(clean)[-keep * 2:].decode("utf-16-le", errors="ignore")


def _empty_to_null(value: str | None) -> str | None:
    if value is None:
        return None
    return None if value.strip(_JS_SPACE) == "" else value


def view(row, tz: str, attachment_count: int, full: bool) -> dict:
    number = _try_decrypt(row.documentNumberEnc)
    days = dates.days_until(row.expiryDate, tz) if row.expiryDate else None
    window = row.reminderDaysBefore if row.reminderDaysBefore is not None else DEFAULT_EXPIRY_WINDOW_DAYS
    if days is None:
        status = "noExpiry"
    elif days < 0:
        status = "expired"
    else:
        status = "expiringSoon" if days <= window else "valid"
    return {
        "id": row.id,
        "name": row.name,
        "type": row.type,
        "documentNumber": None if number is None else (number if full else mask_number(number)),
        "documentNumberMasked": number is not None and not full,
        "hasDocumentNumber": row.documentNumberEnc is not None,
        "notes": _try_decrypt(row.notesEnc) if full else None,
        "hasNotes": row.notesEnc is not None,
        "issuer": row.issuer,
        "holderName": row.holderName,
        "issueDate": row.issueDate,
        "expiryDate": row.expiryDate,
        "reminderDaysBefore": row.reminderDaysBefore,
        "daysRemaining": days,
        "status": status,
        "attachmentCount": attachment_count,
        "createdAt": row.createdAt,
        "updatedAt": row.updatedAt,
    }


async def _attachment_counts(user_id: str, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    groups = await db.attachment.group_by(
        by=["entityId"], where={"userId": user_id, "entityType": "DOCUMENT", "entityId": {"in": ids}}, count={"_all": True}
    )
    return {g["entityId"]: g["_count"]["_all"] for g in groups}


async def _tz(user) -> str:
    return (await ensure_os_profile(user)).timezone


def _assert_dates(issue, expiry) -> None:
    if issue and expiry and dates._utc(expiry) < dates._utc(issue):
        raise nest_error(400, "expiryDate must be on or after issueDate")


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    type: TypeName | None = None
    documentNumber: StrictStr | None = None
    notes: StrictStr | None = Field(default=None, max_length=5000)
    issuer: StrictStr | None = None
    holderName: StrictStr | None = None
    issueDate: StrictStr | None = None
    expiryDate: StrictStr | None = None
    reminderDaysBefore: StrictInt | None = Field(default=None, ge=0, le=730)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)

    @field_validator("documentNumber")
    @classmethod
    def _number(cls, v):
        return validators.trimmed(v, "documentNumber", 0, 100)

    @field_validator("issuer", "holderName")
    @classmethod
    def _names(cls, v, info):
        return validators.trimmed(v, info.field_name, 0, 120)

    @field_validator("issueDate", "expiryDate")
    @classmethod
    def _dates(cls, v, info):
        if v is not None and not dates.is_iso_date_string(v):
            raise ValueError(f"{info.field_name} must be a valid ISO 8601 date string")
        return v


class CreateBody(_Fields):
    name: StrictStr


class UpdateBody(_Fields):
    pass


async def _owned(user_id: str, id: str):
    row = await db.document.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Document not found")
    return row


@router.get("")
async def list_documents(request: Request, user=Depends(plus_user)):
    q = request.query_params
    errors = unknown_params(request, ("type", "status", "q"))
    types = _query_list(request, "type", TYPES, errors)
    statuses = _query_list(request, "status", STATUSES, errors)
    text = q.get("q")
    if text is not None and len(text) > 100:
        errors.append("q must be shorter than or equal to 100 characters")
    if errors:
        raise nest_error(400, errors)
    tz = await _tz(user)
    where: dict = {"userId": user.id}
    if types:
        where["type"] = {"in": types}
    if text:
        contains = {"contains": text, "mode": "insensitive"}
        where["OR"] = [{"name": contains}, {"issuer": contains}, {"holderName": contains}]
    rows = await db.document.find_many(where=where, order=[{"expiryDate": {"sort": "asc", "nulls": "last"}}, {"name": "asc"}])
    counts = await _attachment_counts(user.id, [r.id for r in rows])
    items = [view(r, tz, counts.get(r.id, 0), False) for r in rows]
    if statuses:
        items = [d for d in items if d["status"] in statuses]
    return {"items": items, "total": len(items)}


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    row = await _owned(user.id, id)
    counts = await _attachment_counts(user.id, [row.id])
    return view(row, await _tz(user), counts.get(row.id, 0), True)


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    issue, expiry = dates.parse_date_only(body.issueDate), dates.parse_date_only(body.expiryDate)
    _assert_dates(issue, expiry)
    row = await db.document.create(
        data={
            "userId": user.id,
            "name": body.name,
            "type": body.type or "OTHER",
            "documentNumberEnc": _encrypt(_empty_to_null(body.documentNumber)),
            "notesEnc": _encrypt(_empty_to_null(body.notes)),
            "issuer": _empty_to_null(body.issuer),
            "holderName": _empty_to_null(body.holderName),
            "issueDate": issue,
            "expiryDate": expiry,
            "reminderDaysBefore": DEFAULT_EXPIRY_WINDOW_DAYS if "reminderDaysBefore" not in body.model_fields_set else body.reminderDaysBefore,
        }
    )
    # the number and notes never go into the activity log
    await log_activity(
        user.id, "document.created", entity_id=row.id, name=row.name, type=row.type,
        expiryDate=dates.format_date_only(row.expiryDate) if row.expiryDate else None,
    )
    return view(row, await _tz(user), 0, True)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    sent = body.model_fields_set
    if "name" in sent and body.name is None:
        raise nest_error(400, "name cannot be empty")
    issue = dates.parse_date_only(body.issueDate) if "issueDate" in sent else existing.issueDate
    expiry = dates.parse_date_only(body.expiryDate) if "expiryDate" in sent else existing.expiryDate
    _assert_dates(issue, expiry)
    data: dict = {}
    if "name" in sent:
        data["name"] = body.name
    if body.type is not None:
        data["type"] = body.type
    if "documentNumber" in sent:
        data["documentNumberEnc"] = _encrypt(_empty_to_null(body.documentNumber))
    if "notes" in sent:
        data["notesEnc"] = _encrypt(_empty_to_null(body.notes))
    for field in ("issuer", "holderName"):
        if field in sent:
            data[field] = _empty_to_null(getattr(body, field))
    if "issueDate" in sent:
        data["issueDate"] = issue
    if "expiryDate" in sent:
        data["expiryDate"] = expiry
    if "reminderDaysBefore" in sent:
        data["reminderDaysBefore"] = body.reminderDaysBefore
    row = await db.document.update(where={"id": existing.id}, data=data or {"name": existing.name})
    await log_activity(
        user.id, "document.updated", entity_id=row.id, name=row.name,
        expiryDate=dates.format_date_only(row.expiryDate) if row.expiryDate else None,
    )
    counts = await _attachment_counts(user.id, [row.id])
    return view(row, await _tz(user), counts.get(row.id, 0), True)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    """Deletes the document and its attachment files; linked reminders stay, unlinked."""
    existing = await _owned(user.id, id)
    async with db.tx() as tx:
        keys = await attachments.delete_rows(tx, user.id, "DOCUMENT", [existing.id])
        await tx.reminder.update_many(
            where={"userId": user.id, "entityType": "DOCUMENT", "entityId": existing.id}, data={"entityType": None, "entityId": None}
        )
        await tx.document.delete(where={"id": existing.id})
    await attachments.delete_objects(keys)
    await log_activity(user.id, "document.deleted", entity_id=id, name=existing.name, attachmentsDeleted=len(keys))
    return {"ok": True}
