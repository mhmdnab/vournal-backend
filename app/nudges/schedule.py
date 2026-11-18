"""Which nudge moment is due, in the phone's local time (design 2026-11-18). Pure.

Morning at the person's time, midday at 13:00, evening at 20:00, each with a 30-minute window; a
missed window is skipped, never sent late. Quiet hours are 22:00–08:00, except a morning the
person set earlier themselves. Free users get no midday nudge."""

from datetime import datetime, timedelta
from typing import Literal

Moment = Literal["morning", "midday", "evening"]

MIDDAY, EVENING = "13:00", "20:00"
WINDOW = timedelta(minutes=30)
CAP_PLUS, CAP_FREE = 3, 2
QUIET_START, QUIET_END = "22:00", "08:00"


def _start(local_now: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return local_now.replace(hour=h, minute=m, second=0, microsecond=0)


def due_moment(local_now: datetime, morning: str) -> Moment | None:
    """The moment whose window holds local_now; when windows overlap, the earlier start wins."""
    starts = sorted(((_start(local_now, t), name) for name, t in (("morning", morning), ("midday", MIDDAY), ("evening", EVENING))),
                    key=lambda pair: pair[0])
    for start, name in starts:
        if start <= local_now < start + WINDOW:
            return name  # type: ignore[return-value]
    return None


def in_quiet_hours(local_now: datetime) -> bool:
    hhmm = local_now.strftime("%H:%M")
    return hhmm >= QUIET_START or hhmm < QUIET_END


def allowed(moment: Moment, local_now: datetime) -> bool:
    return moment == "morning" or not in_quiet_hours(local_now)


def moments_for(plus: bool) -> tuple[Moment, ...]:
    return ("morning", "midday", "evening") if plus else ("morning", "evening")
