"""/api/attachments (port of vournalOS AttachmentsService and its controller).

Files attached to a task, document, trip… live in the app's private R2 bucket under
"os/attachments/<userId>/<random>", not in Postgres as in NestJS. They're served only
through the owner's download route; lists never carry the bytes.
"""

import re
import uuid
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from app import storage
from app.db import db
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.jsparse import _JS_SPACE
from app.os.common.ownership import ENTITY_TYPE_MODEL, assert_entity
from app.os.common.query import unknown_params

router = nest_router("/attachments")

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ENTITY_TYPES = tuple(ENTITY_TYPE_MODEL)
# documents, images, office files, plain text
ALLOWED_MIME = {
    "application/pdf", "image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif",
    "text/plain", "text/csv", "text/markdown", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/zip",
}
_FIELDS = ("entityType", "entityId")
_UNSAFE = re.compile(r'[\\/\x00-\x1f"<>|:*?]')


def view(row) -> dict:
    return {f: getattr(row, f) for f in ("id", "entityType", "entityId", "fileName", "mimeType", "size", "createdAt")}


def _target_errors(values: dict) -> list[str]:
    """AttachmentTargetDto: @IsEnum(EntityType) entityType, @IsString @MaxLength(64) entityId."""
    errors = []
    if values.get("entityType") not in ENTITY_TYPES:
        errors.append(f"entityType must be one of the following values: {', '.join(ENTITY_TYPES)}")
    entity_id = values.get("entityId")
    if not isinstance(entity_id, str) or len(entity_id) > 64:
        errors.append("entityId must be shorter than or equal to 64 characters")
    if not isinstance(entity_id, str):
        errors.append("entityId must be a string")
    return errors


_FORM_ESCAPES = re.compile(r"%0A|%0D|%22", re.IGNORECASE)


def _upload_name(raw: str | None) -> str:
    """What multer handed NestJS: busboy keeps only the part after the last / or \\
    ("." and ".." become empty), then multer undoes the HTML spec's three escapes."""
    name = re.split(r"[/\\]", raw or "")[-1]
    name = "" if name in (".", "..") else name
    return _FORM_ESCAPES.sub(lambda m: {"%0A": "\n", "%0D": "\r"}.get(m[0].upper(), '"'), name)


def _file_name(name: str | None) -> str:
    """No path or control characters; at most 200 UTF-16 units, as JS's slice counted."""
    cleaned = _UNSAFE.sub("_", _upload_name(name)).strip(_JS_SPACE) or "file"
    return cleaned.encode("utf-16-le")[:400].decode("utf-16-le", errors="ignore")


@router.get("")
async def list_attachments(request: Request, user=Depends(plus_user)):
    q = request.query_params
    errors = unknown_params(request, _FIELDS) + _target_errors({k: q.get(k) for k in _FIELDS})
    if errors:
        raise nest_error(400, errors)
    await assert_entity(q.get("entityType"), q.get("entityId"), user.id)
    rows = await db.attachment.find_many(
        where={"userId": user.id, "entityType": q.get("entityType"), "entityId": q.get("entityId")}, order={"createdAt": "desc"}
    )
    return {"items": [view(r) for r in rows], "total": len(rows)}


@router.post("", status_code=201)
async def create(request: Request, user=Depends(plus_user)):
    form = await request.form(max_files=1)
    files = [(k, v) for k, v in form.multi_items() if isinstance(v, UploadFile)]
    if any(name != "file" for name, _ in files):
        raise nest_error(400, "Unexpected field")
    upload = files[0][1] if files else None
    # multer stops an oversize file before anything else is looked at
    if upload is not None and (upload.size or 0) > MAX_ATTACHMENT_BYTES:
        raise nest_error(413, "File too large")
    values = {k: v for k, v in form.multi_items() if not isinstance(v, UploadFile)}
    errors = [f"property {k} should not exist" for k in values if k not in _FIELDS] + _target_errors(values)
    if errors:
        raise nest_error(400, errors)
    if upload is None:
        raise nest_error(400, "File is required")
    mime = upload.content_type or ""
    if mime not in ALLOWED_MIME:
        raise nest_error(400, "This file type is not allowed")
    entity_type, entity_id = values["entityType"], values["entityId"]
    await assert_entity(entity_type, entity_id, user.id)

    data = await upload.read()
    key = f"os/attachments/{user.id}/{uuid.uuid4().hex}"
    await storage.put_object(key, data, mime)
    try:
        row = await db.attachment.create(
            data={
                "userId": user.id, "entityType": entity_type, "entityId": entity_id,
                "fileName": _file_name(upload.filename), "mimeType": mime, "size": len(data), "storageKey": key,
            }
        )
    except BaseException:
        await storage.delete_object(key)  # no row, no file
        raise
    await log_activity(user.id, "attachment.created", entity_id=entity_id, entity_type=entity_type, fileName=row.fileName)
    return view(row)


async def _owned(user_id: str, id: str):
    row = await db.attachment.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Attachment not found")
    return row


@router.get("/{id}/download")
async def download(id: str, request: Request, user=Depends(plus_user)):
    row = await _owned(user.id, id)
    data = await storage.get_object(row.storageKey)
    if data is None:
        raise nest_error(404, "Attachment not found")
    disposition = "inline" if request.query_params.get("inline") == "1" else "attachment"
    return Response(
        content=data,
        media_type=row.mimeType,
        headers={
            # encodeURIComponent keeps A-Z a-z 0-9 - _ . ! ~ * ' ( )
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(row.fileName, safe=chr(39) + '!()*-._~')}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    row = await _owned(user.id, id)
    await db.attachment.delete(where={"id": row.id})
    await storage.delete_object(row.storageKey)
    return {"id": row.id}
