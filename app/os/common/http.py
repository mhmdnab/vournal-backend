"""Errors and responses for /api in the shape the dashboard reads (NestJS's).

    {"statusCode": 404, "message": "Not found", "error": "Not Found"}

Validation failures are 400 with `message` as a list, like class-validator.
Every other path keeps FastAPI's own handlers, so the app's routes don't change.
"""

import functools
import json
import re
import types
import typing
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
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


def invalid_data() -> HTTPException:
    """What NestJS answered when Prisma refused a value (e.g. null on a required column):
    its database error filter names the status in capitals."""
    return nest_error(400, "Invalid data", error=HTTPStatus.BAD_REQUEST.name)


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


_NUMBER = "must be a number conforming to the specified constraints"
# pydantic's error types in class-validator's words
_TYPE_WORDS = {
    "string_type": "must be a string",
    "int_type": "must be an integer number", "int_parsing": "must be an integer number", "int_from_float": "must be an integer number",
    "bool_type": "must be a boolean value", "bool_parsing": "must be a boolean value",
    "float_type": _NUMBER, "float_parsing": _NUMBER, "finite_number": _NUMBER,
    "list_type": "must be an array",
}


def _unwrap(annotation):
    """X | None → X; list[X] stays a list."""
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if typing.get_origin(annotation) in (typing.Union, types.UnionType) and len(args) == 1:
        return args[0]
    return annotation


def _choices(annotation) -> list[str] | None:
    annotation = _unwrap(annotation)
    return [str(a) for a in typing.get_args(annotation)] if typing.get_origin(annotation) is typing.Literal else None


def _leaf_annotation(model, path: list):
    """The declared type at a body path (fields and list items), or None."""
    annotation = model
    for part in path:
        annotation = _unwrap(annotation)
        if isinstance(part, int):
            args = typing.get_args(annotation)
            annotation = args[0] if args else None
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel) and part in annotation.model_fields:
            annotation = annotation.model_fields[part].annotation
        else:
            return None
    return annotation


def _missing_words(annotation) -> str:
    """What class-validator said about an absent required field, from its type."""
    if (choices := _choices(annotation)) is not None:
        return f"must be one of the following values: {', '.join(choices)}"
    annotation = _unwrap(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "nested property"
    if typing.get_origin(annotation) is list:
        return "must be an array"
    words = {str: "must be a string", int: "must be an integer number", bool: "must be a boolean value", object: _NUMBER}
    return words.get(annotation if isinstance(annotation, type) else None, "should not be empty")


def _validation_message(error: dict, model=None) -> str:
    """One pydantic error in class-validator's words. NestJS's ValidationPipe prefixes a
    nested property's message with its parent path ("schedule.day must …")."""
    path = list(error.get("loc", ())[1:])
    kind, msg, ctx = error.get("type"), error.get("msg", ""), error.get("ctx") or {}
    if not path:
        return msg.removeprefix("Value error, ")
    leaf, parent = path[-1], path[:-1]
    if isinstance(leaf, int):  # a list item: class-validator's "each value in …"
        prop, parent = f"each value in {parent[-1]}" if parent else "each value", parent[:-1]
    else:
        prop = str(leaf)
    if kind == "value_error":
        core = msg.removeprefix("Value error, ")
    elif kind == "extra_forbidden":
        core = f"property {prop} should not exist"
    elif kind in ("literal_error", "enum"):
        choices = re.findall(r"'([^']*)'", ctx.get("expected", ""))
        core = f"{prop} must be one of the following values: {', '.join(choices)}"
    elif kind == "greater_than_equal":
        core = f"{prop} must not be less than {ctx.get('ge')}"
    elif kind == "less_than_equal":
        core = f"{prop} must not be greater than {ctx.get('le')}"
    elif kind == "string_too_long":
        core = f"{prop} must be shorter than or equal to {ctx.get('max_length')} characters"
    elif kind == "string_too_short":
        core = f"{prop} must be longer than or equal to {ctx.get('min_length')} characters"
    elif kind == "too_long":
        core = f"{prop} must contain no more than {ctx.get('max_length')} elements"
    elif kind in ("model_type", "model_attributes_type", "dict_type"):
        core = f"nested property {prop} must be either object or array"
    elif kind == "missing":
        words = _missing_words(_leaf_annotation(model, path)) if model is not None else "should not be empty"
        core = f"nested property {prop} must be either object or array" if words == "nested property" else f"{prop} {words}"
    else:
        core = f"{prop} {_TYPE_WORDS.get(kind, msg)}"
    return ".".join(str(p) for p in parent) + "." + core if parent else core


def _body_model(request: Request):
    field = getattr(request.scope.get("route"), "body_field", None)
    annotation = getattr(getattr(field, "field_info", None), "annotation", None) or getattr(field, "type_", None)
    return annotation if isinstance(annotation, type) and issubclass(annotation, BaseModel) else None


async def _validation_error(request: Request, exc: RequestValidationError):
    if not _is_api(request):
        return await request_validation_exception_handler(request, exc)
    model = _body_model(request)
    return _body(400, [_validation_message(e, model) for e in exc.errors()])


def _unique_message(exc: UniqueViolationError) -> str:
    target = (exc.meta or {}).get("target") or []
    fields = [f for f in (target if isinstance(target, list) else [target]) if f != "userId"]
    return f"A record with this {', '.join(fields)} already exists" if fields else "Already exists"


def _database_error(status: int, message):
    async def handler(request: Request, exc: Exception):
        if not _is_api(request):
            raise exc  # unhandled before /api existed: still a plain 500
        # NestJS's database error filter used the status's name: "CONFLICT", "NOT_FOUND".
        return _body(status, message(exc) if callable(message) else message, error=HTTPStatus(status).name)

    return handler


def install_nest_errors(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(UniqueViolationError, _database_error(409, _unique_message))
    app.add_exception_handler(RecordNotFoundError, _database_error(404, "Not found"))
    app.add_exception_handler(ForeignKeyViolationError, _database_error(400, "Related record not found"))


class EmptyJsonBody:
    """Express handed NestJS `{}` for a write without a body; FastAPI would call the
    body missing. Only /api writes with no body (or an empty JSON one) are touched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH") or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        content_type = dict(scope["headers"]).get(b"content-type", b"")
        if content_type and not content_type.startswith(b"application/json"):
            return await self.app(scope, receive, send)
        # the body may come in several messages (some an empty chunk); keep them until it's complete
        messages = []
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        if all(m["type"] == "http.request" and not m.get("body") for m in messages):
            messages = [{"type": "http.request", "body": b"{}", "more_body": False}]
            if not content_type:
                scope = {**scope, "headers": [*scope["headers"], (b"content-type", b"application/json")]}

        async def replay():
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)
