"""Expo's push service (design 2026-11-18, notifications that nudge).

send() posts messages in batches of 100 and returns one ticket per message, in order; receipts()
asks for delivery receipts in batches of 1000 (Expo keeps them 24 hours; check after 15 minutes).
DeviceNotRegistered means the token is dead: the caller deletes it. Nothing here raises: a failed
request gives error tickets (or no receipts), so one bad call never stops the nudge job.
EXPO_ACCESS_TOKEN is sent only when set (Expo's optional "enhanced push security")."""

import logging
import os

import httpx

log = logging.getLogger("vournal")

SEND_URL = "https://exp.host/--/api/v2/push/send"
RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
SEND_BATCH, RECEIPT_BATCH = 100, 1000

_transport: httpx.AsyncBaseTransport | None = None  # replaced in tests


def _headers() -> dict:
    headers = {"accept": "application/json", "content-type": "application/json"}
    token = os.environ.get("EXPO_ACCESS_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _error_of(item: dict) -> str | None:
    details = item.get("details")
    return details.get("error") if isinstance(details, dict) else None


# A request that may have reached Expo but got no usable answer (a timeout after sending, a reply
# that isn't Expo's): the push may be on the phone already, so the nudge job doesn't send it again.
# A request Expo surely didn't take (no connection, an error status) gives error None.
NO_ANSWER = "NoAnswer"


class _Unclear(Exception):
    pass


async def _post(client: httpx.AsyncClient, url: str, payload) -> dict | None:
    """The reply, None when Expo surely didn't take it; raises _Unclear when it may have."""
    try:
        response = await client.post(url, json=payload, headers=_headers())
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        log.warning("Expo push request failed: %s", type(exc).__name__)
        return None
    except httpx.HTTPError as exc:
        log.warning("Expo push request failed: %s", type(exc).__name__)
        raise _Unclear from exc
    if response.status_code != 200:
        log.warning("Expo push request failed (%s)", response.status_code)
        return None
    try:
        return response.json()
    except ValueError:
        log.warning("Expo push reply wasn't JSON")
        raise _Unclear from None


def new_client() -> httpx.AsyncClient:
    """One connection pool, for a caller sending many people's nudges in one go."""
    return httpx.AsyncClient(timeout=15.0, transport=_transport)


async def send(messages: list[dict], client: httpx.AsyncClient | None = None) -> list[dict]:
    """One ticket per message, in order: {"status": "ok", "id"} or {"status": "error", "error"}.
    `client`: a pool to reuse (the nudge job's tick); otherwise one is opened for this call."""
    tickets: list[dict] = []
    if not messages:
        return tickets
    if client is None:
        async with new_client() as own:
            return await send(messages, own)
    for start in range(0, len(messages), SEND_BATCH):
        batch = messages[start:start + SEND_BATCH]
        try:
            body = await _post(client, SEND_URL, batch)
        except _Unclear:
            tickets += [{"status": "error", "error": NO_ANSWER} for _ in batch]
            continue
        if body is None:
            tickets += [{"status": "error", "error": None} for _ in batch]
            continue
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(batch):
            tickets += [{"status": "error", "error": NO_ANSWER} for _ in batch]  # an answer, but not Expo's
            continue
        for item in data:
            if isinstance(item, dict) and item.get("status") == "ok" and item.get("id"):
                tickets.append({"status": "ok", "id": item["id"]})
            else:
                tickets.append({"status": "error", "error": _error_of(item) if isinstance(item, dict) else None})
    return tickets


async def receipts(ids: list[str]) -> dict[str, dict] | None:
    """id -> {"status", "error"}; ids without a receipt yet are left out; None when no request got
    an answer (so the caller can try again later rather than give up)."""
    out: dict[str, dict] = {}
    if not ids:
        return out
    answered = False
    async with httpx.AsyncClient(timeout=15.0, transport=_transport) as client:
        for start in range(0, len(ids), RECEIPT_BATCH):
            try:
                body = await _post(client, RECEIPTS_URL, {"ids": ids[start:start + RECEIPT_BATCH]})
            except _Unclear:
                continue  # receipts are only read: an unclear answer is no answer
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                continue
            answered = True
            for rid, item in data.items():
                if isinstance(item, dict):
                    out[rid] = {"status": item.get("status"), "error": _error_of(item)}
    return out if answered else None
