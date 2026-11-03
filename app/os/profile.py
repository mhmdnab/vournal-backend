"""The vournal OS side of an app account, made the first time it reaches /api.

vournalOS created these at sign-up; accounts are now made in the iOS app, so the
same rows are created here, once, the first time the web sees the account.
"""

from decimal import Decimal

from prisma import Json
from prisma.errors import UniqueViolationError

from app.db import db
from app.os.common.defaults import (
    DEFAULT_CURRENCY,
    DEFAULT_DASHBOARD_WIDGETS,
    DEFAULT_ENABLED_SECTIONS,
    DEFAULT_NOTIFICATION_PREFS,
    DEFAULT_RATES_PER_USD,
    default_category_rows,
)
from app.os.common.i18n import default_name

_LANG = "en"  # the web can switch it; Arabic renames the seeded rows (settings.py)


async def ensure_os_profile(user):
    """The user's UserSettings, creating the profile first if this is their first
    visit. Safe when two requests race: the loser re-reads the winner's row."""
    existing = await db.usersettings.find_unique(where={"userId": user.id})
    if existing is not None:
        return existing
    currency = DEFAULT_CURRENCY
    try:
        async with db.tx() as tx:
            settings = await tx.usersettings.create(
                data={
                    "userId": user.id,
                    "defaultCurrency": currency,
                    "timezone": user.timezone or "UTC",
                    "language": _LANG,
                    "enabledSections": DEFAULT_ENABLED_SECTIONS,
                    "dashboardWidgets": Json(DEFAULT_DASHBOARD_WIDGETS),
                    "notificationPrefs": Json(DEFAULT_NOTIFICATION_PREFS),
                }
            )
            await tx.category.create_many(
                data=[{"userId": user.id, **row} for row in default_category_rows(_LANG)]
            )
            await tx.exchangerate.create_many(
                data=[
                    {"userId": user.id, "currency": code, "ratePerUsd": Decimal(rate)}
                    for code, rate in DEFAULT_RATES_PER_USD.items()
                ]
            )
            await tx.account.create(
                data={
                    "userId": user.id,
                    "name": default_name("Cash", _LANG),
                    "type": "CASH",
                    "currency": currency,
                    "icon": "wallet",
                    "color": "#10b981",
                }
            )
            await tx.shoppinglist.create(
                data={
                    "userId": user.id,
                    "name": default_name("Grocery", _LANG),
                    "icon": "shopping-basket",
                    "color": "#22c55e",
                    "currency": currency,
                }
            )
        return settings
    except UniqueViolationError:
        # Another request created the profile between our read and our insert.
        return await db.usersettings.find_unique_or_raise(where={"userId": user.id})
