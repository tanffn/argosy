"""Exchange schedules, not downloaded-bar presence, own comparison clocks."""
from datetime import date, timedelta
from functools import lru_cache

import exchange_calendars as calendars


@lru_cache(maxsize=16)
def calendar(name="XNYS"):
    return calendars.get_calendar(name)


def next_session(day):
    after = date.fromisoformat(day) + timedelta(days=1)
    return calendar().date_to_session(after.isoformat(), direction="next").date().isoformat()


def sessions(start, end):
    if start > end:
        return []
    return [stamp.date().isoformat() for stamp in calendar().sessions_in_range(start, end)]


def close_at(day, name="XNYS"):
    return calendar(name).session_close(day).to_pydatetime()


def listing_calendar(provider):
    return next((name for suffix, name in {".L": "XLON", ".SW": "XSWX", ".DE": "XFRA",
        ".AS": "XAMS", ".MI": "XMIL"}.items() if str(provider).endswith(suffix)), "XNYS")
