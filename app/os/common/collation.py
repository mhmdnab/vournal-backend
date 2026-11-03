"""String#localeCompare order for titles and names (ICU, as Node sorted them):
the Unicode Collation Algorithm through pyuca, checked against Node in
tests/vournal_os/golden/jsparse.json. Equal keys keep their order, as JS's stable
sort did when localeCompare returned 0."""

import functools

import pyuca


@functools.cache
def _collator() -> pyuca.Collator:
    return pyuca.Collator()  # loads the Unicode table once, on first use


def locale_key(text: str) -> tuple:
    return _collator().sort_key(text or "")
