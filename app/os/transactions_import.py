"""Bank-statement import helpers (port of vournalOS import.util).

Statement descriptions carry noise that changes on every line ("POS 4821
SPINNEYS BEIRUT 12/10"), so titles are compared in a normalised form: lower case,
digits and punctuation removed, spaces collapsed.
"""

import re

_DIGITS = re.compile(r"[\d٠-٩]+")


def normalize_title(title: str | None) -> str:
    text = _DIGITS.sub(" ", (title or "").lower())
    text = "".join(ch if ch.isalpha() or ch.isspace() else " " for ch in text)  # \p{L} and spaces
    return re.sub(r"\s+", " ", text).strip()


def import_key(day: str, kind: str, amount: float, title: str | None) -> str:
    return f"{day}|{kind}|{amount:.2f}|{normalize_title(title)}"


def mark_duplicates(existing: list[tuple], incoming: list[tuple]) -> list[bool]:
    """Which incoming rows already exist. A multiset: two identical coffees in the
    file and one stored → the first is a duplicate, the second is new."""
    counts: dict[str, int] = {}
    for row in existing:
        key = import_key(*row)
        counts[key] = counts.get(key, 0) + 1
    out = []
    for row in incoming:
        key = import_key(*row)
        left = counts.get(key, 0)
        if left <= 0:
            out.append(False)
        else:
            counts[key] = left - 1
            out.append(True)
    return out


def suggest_categories(history, incoming: list[tuple]) -> list[str | None]:
    """"This text → that category" learned from the user's history (newest first,
    first hit wins); None when the text was never categorised before."""
    learned: dict[str, str] = {}
    for h in history:
        if not h.categoryId:
            continue
        key = f"{h.type}|{normalize_title(h.title)}"
        if key.endswith("|"):
            continue
        learned.setdefault(key, h.categoryId)
    out = []
    for title, kind in incoming:
        norm = normalize_title(title)
        out.append(learned.get(f"{kind}|{norm}") if norm else None)
    return out
