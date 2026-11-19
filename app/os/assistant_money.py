"""The assistant's budget and recurring payment tools (design 2026-11-18, the assistant in every
section). Paying and skipping a payment are final: the routes have no reverse, and a loan's
payment also records a payment on its debt (ruling R4)."""

from app.db import db
from app.os import assistant_sections, budgets, recurring_payments
from app.os import assistant_tools as t
from app.os.assistant_tools import Kind, ToolError
from app.os.common.http import nest_error
from app.os.common.serialize import to_json

ACTIONS = ("pay", "skip", "pause", "cancel", "reactivate")
STATUS_ACTIONS = {"pause": recurring_payments.pause, "cancel": recurring_payments.cancel, "reactivate": recurring_payments.reactivate}


def _int(v) -> bool:
    return t._number(v) and float(v).is_integer()


def _budget_link(id: str) -> str:
    return f"/finance/budgets?id={id}"


def _payment_link(id: str) -> str:
    return f"/finance/recurring?id={id}"


def _budget_plain(i: dict) -> dict:
    plain: dict = {}
    if i.get("amount") is not None:
        plain["amount"] = i["amount"]
    if i.get("period") in ("MONTHLY", "YEARLY"):
        plain["period"] = i["period"]
    if t._text(i, "name"):
        plain["name"] = t._text(i, "name")
    if _int(i.get("alert_percent")):
        plain["alertThreshold"] = int(i["alert_percent"])
    if t._text(i, "currency"):
        plain["currency"] = t._text(i, "currency").upper()
    return plain


async def _create_budget(i: dict, ctx) -> dict:
    if i.get("amount") is None:
        raise ToolError('"amount" is required')
    plain = _budget_plain(i)
    if t._text(i, "category_id"):
        plain["categoryId"] = t._text(i, "category_id")
    b = await budgets.create(t._check(budgets.CreateBody, plain), user=ctx.user)
    return t._done({"kind": "create", "entityType": "budget", "entityId": b["id"], "label": b["name"], "link": _budget_link(b["id"]),
                    "undo": {"op": "delete", "type": "budget", "id": b["id"]}},
                   {"id": b["id"], "name": b["name"], "amount": float(b["amount"]), "currency": b["currency"], "period": b["period"]})


async def _update_budget(i: dict, ctx) -> dict:
    id = t._req_str(i.get("id"), "id")
    plain = _budget_plain(i)
    plain.pop("currency", None)
    if isinstance(i.get("active"), bool):
        plain["isActive"] = i["active"]
    if not plain:
        raise ToolError("Nothing to change")
    before = await t.fields_now("budget", id, plain, ctx)
    b = await budgets.update(id, t._check(budgets.UpdateBody, plain), user=ctx.user)
    return t._done({"kind": "update", "entityType": "budget", "entityId": id, "label": b["name"], "link": _budget_link(id),
                    "undo": await t._fields_undo("budget", id, before, ctx)},
                   {"id": id, "name": b["name"], "amount": float(b["amount"]), "period": b["period"], "active": b["isActive"]})


def _payment_plain(i: dict) -> dict:
    plain: dict = {}
    if t._text(i, "name"):
        plain["name"] = t._text(i, "name")
    if i.get("amount") is not None:
        plain["amount"] = i["amount"]
    repeat = t.read_repeat(i.get("repeat"))
    if repeat:
        plain["rrule"] = repeat["rrule"]
    if t._text(i, "category_id"):
        plain["categoryId"] = t._text(i, "category_id")
    if t._text(i, "account_id"):
        plain["accountId"] = t._text(i, "account_id")
    if _int(i.get("remind_days_before")):
        plain["reminderDaysBefore"] = int(i["remind_days_before"])
    return plain


def _payment_result(p: dict) -> dict:
    return {"id": p["id"], "name": p["name"], "amount": float(p["amount"]), "currency": p["currency"], "kind": p["kind"],
            "nextDate": to_json(p["nextDate"])[:10], "status": p["status"]}


async def _create_recurring_payment(i: dict, ctx) -> dict:
    plain = {"name": t._req_str(i.get("name"), "name"), **_payment_plain(i)}
    if "amount" not in plain:
        raise ToolError('"amount" is required')
    if i.get("kind") in recurring_payments.KINDS:
        plain["kind"] = i["kind"]
    if t.opt_date(i.get("first_date")):
        plain["startDate"] = t.opt_date(i["first_date"])
    if t._text(i, "currency"):
        plain["currency"] = t._text(i, "currency").upper()
    p = await recurring_payments.create(t._check(recurring_payments.CreateBody, plain), user=ctx.user)
    return t._done({"kind": "create", "entityType": "recurring_payment", "entityId": p["id"], "label": p["name"], "link": _payment_link(p["id"]),
                    "undo": {"op": "delete", "type": "recurring_payment", "id": p["id"]}}, _payment_result(p))


async def _update_recurring_payment(i: dict, ctx) -> dict:
    id = t._req_str(i.get("id"), "id")
    action = i.get("action")
    if action is not None and action not in ACTIONS:
        raise ToolError(f"action must be one of {', '.join(ACTIONS)}")
    if action == "pay":
        return await _pay(id, i, ctx)
    plain = _payment_plain(i)
    if t.opt_date(i.get("next_date")):
        plain["nextDate"] = t.opt_date(i["next_date"])
    if action and plain:
        raise ToolError("Use action alone, or change fields — not both")
    if action == "skip":
        p = await recurring_payments.skip(id, user=ctx.user)  # final: no reverse (ruling R4)
        return t._done({"kind": "update", "entityType": "recurring_payment", "entityId": id, "label": p["name"], "link": _payment_link(id),
                        "undo": None}, {**_payment_result(p), "note": "Skipped; this can't be undone."})
    if action:
        before = await t.fields_now("recurring_payment", id, {"status", "nextDate"}, ctx)
        p = await STATUS_ACTIONS[action](id, user=ctx.user)
    elif not plain:
        raise ToolError("Nothing to change")
    else:
        before = await t.fields_now("recurring_payment", id, plain, ctx)
        p = await recurring_payments.update(id, t._check(recurring_payments.UpdateBody, plain), user=ctx.user)
    return t._done({"kind": "update", "entityType": "recurring_payment", "entityId": id, "label": p["name"], "link": _payment_link(id),
                    "undo": await t._fields_undo("recurring_payment", id, before, ctx)}, _payment_result(p))


async def _pay(id: str, i: dict, ctx) -> dict:
    plain: dict = {}
    if t.opt_date(i.get("date")):
        plain["date"] = t.opt_date(i["date"])
    if i.get("amount") is not None:
        plain["amount"] = i["amount"]
    if t._text(i, "account_id"):
        plain["accountId"] = t._text(i, "account_id")
    out = await recurring_payments.pay(id, t._check(recurring_payments.PayBody, plain), user=ctx.user)
    p, tx = out["payment"], out["transaction"]
    # final: the expense, the next date and (for a loan) its debt payment have no reverse (ruling R4)
    return t._done({"kind": "complete", "entityType": "recurring_payment", "entityId": id, "label": p["name"], "link": _payment_link(id),
                    "undo": None},
                   {**_payment_result(p), "paid": float(tx["amount"]), "transactionId": tx["id"], "note": "Recorded; this can't be undone."})


# ----- item types --------------------------------------------------------------------------


async def _budget_row(user_id: str, id: str):
    row = await db.budget.find_first(where={"id": id, "userId": user_id}, include={"category": True})
    if row is None:
        raise nest_error(404, "Budget not found")
    return row


async def _restore_budget(ctx, id: str, before: dict) -> None:
    await budgets.update(id, t._check(budgets.UpdateBody, before), user=ctx.user)


async def _restore_payment(ctx, id: str, before: dict) -> None:
    await recurring_payments.update(id, t._check(recurring_payments.UpdateBody, before), user=ctx.user)


def _budget_label(row) -> str:
    return row.name or (row.category.name if row.category else budgets.OVERALL["en"])


KINDS = {
    "budget": Kind(model="Budget", find=_budget_row, label=_budget_label, link=lambda r: _budget_link(r.id), delete=budgets.remove,
                   restore=_restore_budget),
    "recurring_payment": Kind(model="RecurringPayment", find=recurring_payments._owned, label=lambda r: r.name, link=lambda r: _payment_link(r.id),
                              delete=recurring_payments.remove, restore=_restore_payment, date_only=frozenset({"startDate", "nextDate"})),
}
TOOLS = {"create_budget": _create_budget, "update_budget": _update_budget, "create_recurring_payment": _create_recurring_payment,
         "update_recurring_payment": _update_recurring_payment}

t.register(TOOLS, KINDS)
assistant_sections.SECTION_OF.update({name: "finance" for name in TOOLS})
