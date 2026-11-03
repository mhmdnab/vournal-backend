"""Query-string parsing as vournalOS's DTO transforms did it."""


def string_list(value) -> list[str] | None:
    """?types=task,note or ?types=task&types=note → ["task", "note"] (ToStringArray)."""
    if value is None or value == "" or value == []:
        return None
    parts = value if isinstance(value, list) else [value]
    out = [piece.strip() for part in parts for piece in str(part).split(",")]
    return [p for p in out if p] or None
