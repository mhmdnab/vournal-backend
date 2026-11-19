"""The assistant's meal planner and recipe tools (design 2026-11-18, the assistant in every section).
Each runs the section's own route with its own body check; plan_meal is for meals planned ahead
(log_food is for food already eaten)."""

from app.os import assistant_sections, meal_plan, recipes
from app.os import assistant_tools as t
from app.os.assistant_tools import Kind, ToolError
from app.os.common.jsparse import js_slice
from app.os.common.serialize import to_json

MEAL_FIELDS = (("date", "date"), ("meal", "mealType"), ("title", "title"), ("recipe_id", "recipeId"), ("servings", "servings"),
               ("notes", "notes"))
RECIPE_TEXT = (("name", "name"), ("description", "description"), ("instructions", "instructions"), ("source_url", "sourceUrl"))
RECIPE_INTS = (("servings", "servings"), ("prep_minutes", "prepMinutes"), ("cook_minutes", "cookMinutes"))


def _day(v) -> str:
    return to_json(v)[:10]


def _meal_link(day: str) -> str:
    return f"/meals?date={day}"


def _int(v) -> bool:
    return t._number(v) and float(v).is_integer()


def _meal_plain(i: dict, update: bool) -> dict:
    plain: dict = {}
    if t.opt_date(i.get("date")):
        plain["date"] = t.opt_date(i["date"])
    elif not update:
        plain["date"] = t._req_date(i.get("date"), "date")
    if i.get("meal") in t.MEALS:
        plain["mealType"] = i["meal"]
    if t._text(i, "title"):
        plain["title"] = js_slice(t._text(i, "title"), 200)
    if t._text(i, "recipe_id"):
        plain["recipeId"] = t._text(i, "recipe_id")
    if _int(i.get("servings")):
        plain["servings"] = int(i["servings"])
    if isinstance(i.get("notes"), str):
        plain["notes"] = i["notes"].strip() or None
    return plain


async def _plan_meal(i: dict, ctx) -> dict:
    e = await meal_plan.create(t._check(meal_plan.CreateBody, _meal_plain(i, False)), user=ctx.user)
    day = _day(e["date"])
    return t._done({"kind": "create", "entityType": "meal_plan_entry", "entityId": e["id"], "label": e["title"], "link": _meal_link(day),
                    "undo": {"op": "delete", "type": "meal_plan_entry", "id": e["id"]}},
                   {"id": e["id"], "date": day, "meal": e["mealType"], "title": e["title"]})


async def _update_meal_plan(i: dict, ctx) -> dict:
    id = t._req_str(i.get("id"), "id")
    plain = _meal_plain(i, True)
    if not plain:
        raise ToolError("Nothing to change")
    keys = set(plain) | ({"title"} if "recipeId" in plain else set())  # another recipe renames it
    before = await t.fields_now("meal_plan_entry", id, keys, ctx)
    e = await meal_plan.update(id, t._check(meal_plan.UpdateBody, plain), user=ctx.user)
    day = _day(e["date"])
    return t._done({"kind": "update", "entityType": "meal_plan_entry", "entityId": id, "label": e["title"], "link": _meal_link(day),
                    "undo": await t._fields_undo("meal_plan_entry", id, before, ctx)},
                   {"id": id, "date": day, "meal": e["mealType"], "title": e["title"]})


async def _shopping_from_meal_plan(i: dict, ctx) -> dict:
    plain = {"from": t._req_date(i.get("from"), "from"), "to": t._req_date(i.get("to"), "to")}
    if t._text(i, "list_id"):
        plain["listId"] = t._text(i, "list_id")
    out = await meal_plan.generate_shopping(t._check(meal_plan.GenerateBody, plain), user=ctx.user)
    # it adds new items and adds to items already there; the route has no reverse: no Undo (ruling R4)
    return t._done({"kind": "create", "entityType": "shopping_item", "entityId": out["listId"], "label": out["listName"],
                    "link": f"/shopping?list={out['listId']}", "undo": None},
                   {"added": out["added"], "merged": out["merged"], "list": out["listName"], "recipes": out["recipeCount"],
                    "note": "This can't be undone; the user removes items from the list by hand."})


def _ingredients(v) -> list:
    if not isinstance(v, list):
        raise ToolError('"ingredients" must be a list of {name, quantity, unit}')
    out = []
    for item in v:
        if not isinstance(item, dict):
            raise ToolError('"ingredients" must be a list of {name, quantity, unit}')
        row = {"name": item.get("name")}
        if item.get("quantity") is not None:
            row["quantity"] = item["quantity"]
        if item.get("unit") is not None:
            row["unit"] = item["unit"]
        out.append(row)
    return out


def _recipe_plain(i: dict) -> dict:
    plain: dict = {}
    for field, key in RECIPE_TEXT:
        if t._text(i, field):
            plain[key] = t._text(i, field)
    for field, key in RECIPE_INTS:
        if _int(i.get(field)):
            plain[key] = int(i[field])
    if "ingredients" in i:
        plain["ingredients"] = _ingredients(i["ingredients"])
    return plain


async def _add_recipe(i: dict, ctx) -> dict:
    plain = {"name": t._req_str(i.get("name"), "name"), **_recipe_plain(i)}
    r = await recipes.create(t._check(recipes.CreateBody, plain), user=ctx.user)
    return t._done({"kind": "create", "entityType": "recipe", "entityId": r["id"], "label": r["name"], "link": f"/meals?recipe={r['id']}",
                    "undo": {"op": "delete", "type": "recipe", "id": r["id"]}},
                   {"id": r["id"], "name": r["name"], "ingredients": r["ingredientCount"]})


async def _update_recipe(i: dict, ctx) -> dict:
    id = t._req_str(i.get("id"), "id")
    plain = _recipe_plain(i)
    if isinstance(i.get("favorite"), bool):
        plain["isFavorite"] = i["favorite"]
    if not plain:
        raise ToolError("Nothing to change")
    before = await t.fields_now("recipe", id, plain, ctx)
    r = await recipes.update(id, t._check(recipes.UpdateBody, plain), user=ctx.user)
    return t._done({"kind": "update", "entityType": "recipe", "entityId": id, "label": r["name"], "link": f"/meals?recipe={id}",
                    "undo": await t._fields_undo("recipe", id, before, ctx)},
                   {"id": id, "name": r["name"], "favorite": r["isFavorite"], "ingredients": r["ingredientCount"]})


# ----- item types --------------------------------------------------------------------------


async def _restore_meal(ctx, id: str, before: dict) -> None:
    await meal_plan.update(id, t._check(meal_plan.UpdateBody, before), user=ctx.user)


def _read_recipe(row, keys) -> dict:
    out = {}
    for k in keys:
        if k == "ingredients":
            out[k] = [{"name": g.name, **({"quantity": float(g.quantity)} if g.quantity is not None else {}), **({"unit": g.unit} if g.unit else {})}
                      for g in sorted(row.ingredients or [], key=lambda g: g.sortOrder)]
        else:
            out[k] = t._row_value(getattr(row, k), False)
    return out


async def _restore_recipe(ctx, id: str, before: dict) -> None:
    await recipes.update(id, t._check(recipes.UpdateBody, before), user=ctx.user)


KINDS = {
    "meal_plan_entry": Kind(model="MealPlanEntry", find=meal_plan._owned, label=lambda r: r.title, link=lambda r: _meal_link(_day(r.date)),
                            delete=meal_plan.remove, restore=_restore_meal, date_only=frozenset({"date"})),
    "recipe": Kind(model="Recipe", find=recipes._owned, label=lambda r: r.name, link=lambda r: f"/meals?recipe={r.id}",
                   delete=recipes.remove, restore=_restore_recipe, read=_read_recipe),
}
TOOLS = {"plan_meal": _plan_meal, "update_meal_plan": _update_meal_plan, "shopping_from_meal_plan": _shopping_from_meal_plan,
         "add_recipe": _add_recipe, "update_recipe": _update_recipe}

t.register(TOOLS, KINDS)
assistant_sections.SECTION_OF.update({name: "meals" for name in TOOLS})
