"""The built-in food list and exercise METs (port of vournalOS health/foods.catalog.ts).
The rows live in data/foods_catalog.json, copied from the NestJS build by
tests/vournal_os/golden/foods_golden.mjs — never typed by hand."""

import json
import pathlib
import re
import unicodedata

from app.os.common.jsparse import _JS_SPACE

_DATA = json.loads((pathlib.Path(__file__).parent / "data" / "foods_catalog.json").read_text(encoding="utf-8"))
ROWS: list[list] = _DATA["rows"]  # [id, English, Arabic, other spellings, serving EN, serving AR, kcal, protein, carbs, fat]
ACTIVITIES: list[list] = _DATA["activities"]  # [id, English, Arabic, MET]
CATALOG_SIZE = len(ROWS)

_SPACES = re.compile("[" + re.escape(_JS_SPACE) + "]+")
_MARKS = re.compile("[ً-ٰٟـ]")  # Arabic diacritics and hamza marks (after NFKD), tatweel


def norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text.lower())
    s = _MARKS.sub("", s)
    s = re.sub("[أإآ]", "ا", s).replace("ة", "ه").replace("ى", "ي")
    s = re.sub("['’`]", "", s)
    return _SPACES.sub(" ", s).strip(_JS_SPACE)


_INDEX = [{"row": r, "hay": norm(f"{r[1]} {r[2]} {r[3]}"), "en": norm(r[1]), "ar": norm(r[2])} for r in ROWS]


def _view(r: list, lang: str) -> dict:
    return {"id": r[0], "name": r[2] if lang == "ar" else r[1], "serving": r[5] if lang == "ar" else r[4],
            "calories": r[6], "protein": r[7], "carbs": r[8], "fat": r[9]}


def catalog_food(id: str, lang: str) -> dict | None:
    row = next((r for r in ROWS if r[0] == id), None)
    return _view(row, lang) if row else None


def search_catalog(q: str, lang: str, limit: int = 20) -> list[dict]:
    """Every word of the query must appear (any language or spelling); names that start with it rank first."""
    query = norm(q)
    if not query:
        return []
    words = query.split(" ")
    found = [x for x in _INDEX if all(w in x["hay"] for w in words)]

    def rank(x) -> int:
        if x["en"].startswith(query) or x["ar"].startswith(query):
            return 0
        return 1 if any(w.startswith(words[0]) for w in x["hay"].split(" ")) else 2

    return [_view(x["row"], lang) for x in sorted(found, key=rank)[:limit]]


def activity_met(id: str) -> float:
    return next((a[3] for a in ACTIVITIES if a[0] == id), 4)
