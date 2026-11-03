"""Occurrences of an event in a range (port of vournalOS event-occurrences)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.os.calendar_utils import DAY, add_days_str, diff_days_str, local_date_str
from app.os.common import dates, rrule

# Longest supported event: how far back to look for one that started before the range.
MAX_SPAN = timedelta(days=62)


@dataclass
class Occurrence:
    start: datetime  # when it starts (differs from `key` only when it follows a prayer)
    key: datetime  # the series' original start instant: exDate key and occurrence id
    end: datetime  # timed: start + duration; all-day: local midnight of the last day
    start_date: str | None = None  # all-day only, inclusive local dates
    end_date: str | None = None


def expand_event(ev, start: datetime, end: datetime, tz: str, resolve=None) -> list[Occurrence]:
    """Occurrences overlapping [start, end] (inclusive) in the user's timezone."""
    ev_start = dates._utc(ev.startAt)
    ev_end = dates._utc(ev.endAt) if ev.endAt else None
    duration = min(max((ev_end or ev_start) - ev_start, timedelta(0)), MAX_SPAN)
    span_days = (
        max(0, diff_days_str(local_date_str(ev_start, tz), local_date_str(ev_end or ev_start, tz))) if ev.allDay else 0
    )
    if not ev.rrule:
        starts = [ev_start]
    else:
        # Look back so an occurrence that began before the range but still runs is included
        # (prayer-anchored ones can shift a couple of hours: look a little further).
        look_back = (span_days + 1) * DAY if ev.allDay else duration + (timedelta(hours=6) if ev.timeAnchor else timedelta(0))
        starts = rrule.between(
            ev.rrule, ev_start, start - look_back, end, list(ev.exDates or []), tz=tz, until=ev.recurrenceEndAt
        )
    out = []
    for occ_start in starts:
        if ev.allDay:
            start_date = local_date_str(occ_start, tz)
            end_date = add_days_str(start_date, span_days)
            if start_date > local_date_str(end, tz) or end_date < local_date_str(start, tz):
                continue
            out.append(Occurrence(occ_start, occ_start, occ_start + span_days * DAY, start_date, end_date))
        else:
            anchored = resolve(local_date_str(occ_start, tz), ev.timeAnchor) if resolve and ev.timeAnchor else None
            shown = anchored[0] if anchored else occ_start
            occ_end = (anchored[1] if anchored else None) or shown + duration
            if shown > end or occ_end < start:
                continue
            out.append(Occurrence(shown, occ_start, occ_end))
    return out
