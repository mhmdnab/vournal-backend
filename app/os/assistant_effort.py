"""How hard the chat thinks about a message (owner decision, 6 November 2026): "low" for one
clear change or lookup, "medium" when the message needs reasoning or several steps. Chosen from
the message itself, so it costs no extra AI call; tests/vournal_os/test_assistant_effort.py
holds the examples this rule must keep getting right."""

import re

LOW, MEDIUM = "low", "medium"
LONG_MESSAGE = 200  # characters: past this a message is rarely one simple change

_ACTION_EN = r"(remind|add|create|move|delete|remove|log|mark|set|schedule|book|cancel|change|make|put|update|tick)"
_ACTION_AR = r"(ذكر|ضيف|ضيفي|اعمل|انقل|احذف|امسح|سجل|غير|حط)"
_MEDIUM_CUES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(all|every|each|plan|planning|compare|comparison)\b",  # many items, planning, analysis
        r"\b(then|also|afterwards)\b",  # one step after another
        rf"\band\s+{_ACTION_EN}",  # "... and remind me ..."
        r"(?<!\w)(كل|خطة|خطه|قارن|مقارنة|ثم|بعدين|كمان)(?!\w)",
        rf"\sو{_ACTION_AR}",  # "... وذكرني ..."
    )
]


def pick_effort(text: str, last_reply_failed: bool) -> str:
    """The effort for a message's first round."""
    if last_reply_failed or len(text) > LONG_MESSAGE:
        return MEDIUM
    return MEDIUM if any(cue.search(text) for cue in _MEDIUM_CUES) else LOW


def escalate(effort: str, round_uses: int, round_had_error: bool) -> str:
    """The effort for the next round of the same message: medium once a tool call failed or the
    model asked for 3 or more changes at once."""
    return MEDIUM if round_had_error or round_uses >= 3 else effort
