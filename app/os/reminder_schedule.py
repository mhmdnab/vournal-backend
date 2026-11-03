"""Reminder scheduling rules (port of vournalOS reminder-schedule).

A timed reminder fires at remindAt. A date-based one is stored at local midnight
and fires at 09:00 local that day, overdue once the day is over. A snooze
replaces the fire time until it fires. A repeating reminder advances remindAt to
its next occurrence after firing and sets lastFiredAt until acknowledged.
"""

from datetime import datetime

from app.os.calendar_utils import is_local_midnight, local_date_str, local_date_time
from app.os.common import dates, rrule

DATE_BASED_FIRE_TIME = "09:00"


def is_all_day(r, tz: str) -> bool:
    return is_local_midnight(r.remindAt, tz)


def fire_at(r, tz: str) -> datetime:
    if r.snoozedUntil:
        return dates._utc(r.snoozedUntil)
    if is_all_day(r, tz):
        return local_date_time(local_date_str(r.remindAt, tz), DATE_BASED_FIRE_TIME, tz)
    return dates._utc(r.remindAt)


def is_overdue(r, tz: str, now: datetime) -> bool:
    if r.isDone:
        return False
    if r.snoozedUntil:
        return dates._utc(r.snoozedUntil) <= now
    if is_all_day(r, tz):
        return local_date_str(r.remindAt, tz) < local_date_str(now, tz)
    return dates._utc(r.remindAt) <= now


def next_occurrence(r, after: datetime, tz: str, resolve=None) -> datetime | None:
    """Next occurrence strictly after `after`; prayer-anchored ones move to that
    day's prayer time."""
    if not r.rrule:
        return None
    nxt = rrule.next_occurrence(r.rrule, dates._utc(r.remindAt), after, tz=tz)
    if nxt is None or not resolve or not r.timeAnchor:
        return nxt
    resolved = resolve(local_date_str(nxt, tz), r.timeAnchor)
    return resolved[0] if resolved else nxt


def advance_after(r, now: datetime) -> datetime:
    return max(now, dates._utc(r.remindAt))
