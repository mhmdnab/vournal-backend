"""Errors and responses for /api in the shape the dashboard reads (NestJS's).

    {"statusCode": 404, "message": "Not found", "error": "Not Found"}

Validation failures are 400 with `message` as a list, like class-validator.
Every other path keeps FastAPI's own handlers, so the app's routes don't change.
"""

import functools
import json
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.responses import Response
from prisma.errors import ForeignKeyViolationError, RecordNotFoundError, UniqueViolationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.os.common.serialize import to_json


class NestJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(to_json(content), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class NestRoute(APIRoute):
    """An /api route whose return value goes through to_json() before FastAPI's own
    encoder, which would otherwise turn datetimes into "+00:00" and Decimals into
    floats before NestJSONResponse ever saw them. Every /api router uses it."""

    def __init__(self, path: str, endpoint, **kwargs):
        if not getattr(endpoint, "__nest_json__", False):
            original = endpoint

            @functools.wraps(original)
            async def endpoint(*args, **kw):
                result = await original(*args, **kw)
                return result if isinstance(result, Response) else to_json(result)

            endpoint.__nest_json__ = True
        super().__init__(path, endpoint, **kwargs)


def nest_router(prefix: str = "", **kwargs):
    """An APIRouter for a vournal OS section."""
    from fastapi import APIRouter

    return APIRouter(prefix=prefix, route_class=NestRoute, default_response_class=NestJSONResponse, **kwargs)


def nest_error(status: int, message: str | list[str], **extra: Any) -> HTTPException:
    """An HTTPException that /api renders as {statusCode, message, error, **extra}."""
    return HTTPException(status, detail={"message": message, **extra})


def _is_api(request: Request) -> bool:
    path = request.url.path
    return path == "/api" or path.startswith("/api/")


def _body(status: int, message: Any, **extra: Any) -> NestJSONResponse:
    return NestJSONResponse(
        {"statusCode": status, "message": message, "error": HTTPStatus(status).phrase, **extra},
        status_code=status,
    )


async def _http_error(request: Request, exc: StarletteHTTPException):
    if not _is_api(request):
        return await http_exception_handler(request, exc)
    detail = exc.detail
    if isinstance(detail, dict) and "message" in detail:
        extra = {k: v for k, v in detail.items() if k != "message"}
        response = _body(exc.status_code, detail["message"], **extra)
    elif exc.status_code in (404, 405) and not isinstance(exc, HTTPException):
        # No route matched (Starlette raised it). Nest has no 405: it's a 404 too.
        response = _body(404, f"Cannot {request.method} {request.url.path}")
    else:
        response = _body(exc.status_code, detail)
    for name, value in (getattr(exc, "headers", None) or {}).items():
        response.headers[name] = value
    return response


def _validation_message(error: dict) -> str:
    field = ".".join(str(part) for part in error.get("loc", ())[1:]) or "body"
    if error.get("type") == "extra_forbidden":
        return f"property {field} should not exist"
    return f"{field} {error.get('msg', 'is invalid')}"


async def _validation_error(request: Request, exc: RequestValidationError):
    if not _is_api(request):
        return await request_validation_exception_handler(request, exc)
    return _body(400, [_validation_message(e) for e in exc.errors()])


def _unique_message(exc: UniqueViolationError) -> str:
    target = (exc.meta or {}).get("target") or []
    fields = [f for f in (target if isinstance(target, list) else [target]) if f != "userId"]
    return f"A record with this {', '.join(fields)} already exists" if fields else "Already exists"


def _database_error(status: int, message):
    async def handler(request: Request, exc: Exception):
        if not _is_api(request):
            raise exc  # unhandled before /api existed: still a plain 500
        return _body(status, message(exc) if callable(message) else message)

    return handler


def install_nest_errors(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(UniqueViolationError, _database_error(409, _unique_message))
    app.add_exception_handler(RecordNotFoundError, _database_error(404, "Not found"))
    app.add_exception_handler(ForeignKeyViolationError, _database_error(400, "Related record not found"))
