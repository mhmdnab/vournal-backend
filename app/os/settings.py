"""/api/settings (port of vournalOS SettingsService and settings controller).

The stored row is normalised on the way out exactly like NestJS's toView: known
sections and widgets only, in canonical order, with every default filled in.
"""

import re
import zoneinfo
from functools import lru_cache
from typing import Literal

from fastapi import Depends
from prisma import Json
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from app.db import db
from app.os.common.defaults import (
    ALWAYS_ON_SECTIONS,
    CURRENCIES,
    DASHBOARD_WIDGET_IDS,
    DATE_FORMATS,
    DEFAULT_DASHBOARD_WIDGETS,
    DEFAULT_ENABLED_SECTIONS,
    DEFAULT_NOTIFICATION_PREFS,
    LANGUAGE_CODES,
    LANGUAGES,
    SECTION_KEYS,
    SUPPORTED_CURRENCIES,
    TIME_FORMATS,
    WEEK_START_VALUES,
    WEEK_STARTS,
)
from app.os.common.deps import plus_user
from app.os.common.http import nest_router
from app.os.common.i18n import Lang, to_lang, translate_default_name
from app.os.profile import ensure_os_profile

router = nest_router("/settings")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d\Z", re.ASCII)


def normalize_sections(values) -> list[str]:
    wanted = set(values or []) | set(ALWAYS_ON_SECTIONS)
    return [k for k in SECTION_KEYS if k in wanted]


def normalize_widgets(value) -> list[dict]:
    """Known ids only, sorted by order, missing ones appended visible, renumbered."""
    if not isinstance(value, list):
        return [dict(w) for w in DEFAULT_DASHBOARD_WIDGETS]
    seen: set[str] = set()
    stored = []
    for i, w in enumerate(x for x in value if isinstance(x, dict)):
        wid = w.get("id")
        if not isinstance(wid, str) or wid not in DASHBOARD_WIDGET_IDS or wid in seen:
            continue
        seen.add(wid)
        order = w.get("order")
        number = isinstance(order, (int, float)) and not isinstance(order, bool)
        stored.append({"id": wid, "visible": w.get("visible") is not False, "order": order if number else i})
    stored.sort(key=lambda w: w["order"])  # stable, like Array.prototype.sort
    missing = [{"id": wid, "visible": True, "order": 0} for wid in DASHBOARD_WIDGET_IDS if wid not in seen]
    return [{**w, "order": n} for n, w in enumerate(stored + missing)]


def normalize_prefs(value) -> dict:
    out = dict(DEFAULT_NOTIFICATION_PREFS)
    if isinstance(value, dict):
        for key in DEFAULT_NOTIFICATION_PREFS:
            v = value.get(key)
            if key == "dailyDigestTime":
                if key in value and (v is None or (isinstance(v, str) and _HHMM.match(v))):
                    out[key] = v
            elif isinstance(v, bool):
                out[key] = v
    return out


def settings_view(row) -> dict:
    return {
        "defaultCurrency": row.defaultCurrency,
        "language": row.language,
        "timezone": row.timezone,
        "dateFormat": row.dateFormat,
        "timeFormat": row.timeFormat,
        "weekStartsOn": row.weekStartsOn,
        "theme": row.theme,
        "enabledSections": normalize_sections(row.enabledSections),
        "dashboardWidgets": normalize_widgets(row.dashboardWidgets),
        "notificationPrefs": normalize_prefs(row.notificationPrefs),
        "onboardingCompleted": row.onboardingCompleted,
        "aiEnabled": row.aiEnabled,
        "updatedAt": row.updatedAt,
    }


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WidgetBody(_Strict):
    id: Literal[tuple(DASHBOARD_WIDGET_IDS)]  # type: ignore[valid-type]
    visible: StrictBool
    order: StrictInt = Field(ge=0)


class PrefsBody(_Strict):
    inApp: StrictBool | None = None
    push: StrictBool | None = None
    email: StrictBool | None = None
    taskReminders: StrictBool | None = None
    eventReminders: StrictBool | None = None
    bills: StrictBool | None = None
    birthdays: StrictBool | None = None
    documents: StrictBool | None = None
    maintenance: StrictBool | None = None
    habits: StrictBool | None = None
    budgets: StrictBool | None = None
    dailyDigestTime: str | None = None

    @field_validator("dailyDigestTime")
    @classmethod
    def _time(cls, v):
        if v is not None and not _HHMM.match(v):
            raise ValueError("dailyDigestTime must be a time in HH:mm format")
        return v


class SettingsBody(_Strict):
    defaultCurrency: Literal[tuple(SUPPORTED_CURRENCIES)] | None = None  # type: ignore[valid-type]
    language: Literal[tuple(LANGUAGE_CODES)] | None = None  # type: ignore[valid-type]
    timezone: str | None = None
    dateFormat: Literal[tuple(DATE_FORMATS)] | None = None  # type: ignore[valid-type]
    timeFormat: Literal[tuple(TIME_FORMATS)] | None = None  # type: ignore[valid-type]
    weekStartsOn: StrictInt | None = None
    theme: Literal["LIGHT", "DARK", "SYSTEM"] | None = None
    enabledSections: list[Literal[tuple(SECTION_KEYS)]] | None = Field(  # type: ignore[valid-type]
        default=None, max_length=len(SECTION_KEYS)
    )
    dashboardWidgets: list[WidgetBody] | None = Field(default=None, max_length=len(DASHBOARD_WIDGET_IDS))
    notificationPrefs: PrefsBody | None = None
    onboardingCompleted: StrictBool | None = None
    aiEnabled: StrictBool | None = None

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v):
        if v is not None and v not in _timezones():
            raise ValueError("timezone must be a valid IANA time zone")
        return v

    @field_validator("weekStartsOn")
    @classmethod
    def _week(cls, v):
        if v is not None and v not in WEEK_START_VALUES:
            raise ValueError("weekStartsOn must be one of the following values: 0, 1, 6")
        return v


@lru_cache(maxsize=1)
def _timezone_set() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones()) | {"UTC"}


def _timezones() -> frozenset[str]:
    return _timezone_set()


@lru_cache(maxsize=1)
def timezone_list() -> list[str]:
    return ["UTC", *sorted(_timezone_set() - {"UTC"})]


async def translate_default_data(user_id: str, lang: Lang) -> None:
    """Rename seeded rows that still carry their untouched default name into `lang`.
    Anything the user renamed or made is left alone. Never raises (NestJS logged)."""
    try:
        categories = await db.category.find_many(where={"userId": user_id, "isDefault": True})
        accounts = await db.account.find_many(where={"userId": user_id, "type": "CASH"})
        lists = await db.shoppinglist.find_many(where={"userId": user_id})
        async with db.tx() as tx:
            for c in categories:
                name = translate_default_name(c.name, lang)
                if name:
                    await tx.category.update(where={"id": c.id}, data={"name": name})
            for a in accounts:
                name = translate_default_name(a.name, lang) if a.name in ("Cash", "نقدي") else None
                if name:
                    await tx.account.update(where={"id": a.id}, data={"name": name})
            for item in lists:
                name = translate_default_name(item.name, lang) if item.name in ("Grocery", "البقالة") else None
                if name:
                    await tx.shoppinglist.update(where={"id": item.id}, data={"name": name})
    except Exception:  # noqa: BLE001 — a failed rename must not fail the settings change
        import logging

        logging.getLogger("vournal.os").warning("Could not translate default data for %s", user_id)


@router.get("")
async def get_settings(user=Depends(plus_user)):
    return settings_view(await ensure_os_profile(user))


@router.patch("")
async def update_settings(body: SettingsBody, user=Depends(plus_user)):
    current = await ensure_os_profile(user)
    patch = body.model_dump(exclude_unset=True)
    data: dict = {}
    for key in (
        "defaultCurrency",
        "language",
        "timezone",
        "dateFormat",
        "timeFormat",
        "weekStartsOn",
        "theme",
        "onboardingCompleted",
        "aiEnabled",
    ):
        if patch.get(key) is not None:
            data[key] = patch[key]
    if body.enabledSections is not None:
        data["enabledSections"] = normalize_sections(body.enabledSections)
    if body.dashboardWidgets is not None:
        data["dashboardWidgets"] = Json(normalize_widgets([w.model_dump() for w in body.dashboardWidgets]))
    if body.notificationPrefs is not None:
        merged = {**normalize_prefs(current.notificationPrefs), **body.notificationPrefs.model_dump(exclude_unset=True)}
        data["notificationPrefs"] = Json(merged)
    row = await db.usersettings.update(where={"userId": user.id}, data=data)
    if body.language is not None and body.language != current.language:
        await translate_default_data(user.id, to_lang(body.language))
    return settings_view(row)


@router.get("/options")
async def options():
    """Static option lists for pickers. Public, as in NestJS."""
    return {
        "currencies": CURRENCIES,
        "sections": SECTION_KEYS,
        "defaultEnabledSections": DEFAULT_ENABLED_SECTIONS,
        "dashboardWidgets": DASHBOARD_WIDGET_IDS,
        "dateFormats": DATE_FORMATS,
        "languages": LANGUAGES,
        "weekStarts": WEEK_STARTS,
        "timezones": timezone_list(),
    }
