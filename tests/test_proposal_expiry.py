from datetime import UTC, date, datetime, timedelta

from argosy.services.proposal_expiry import proposal_expiry_reason


def test_expiry_is_timezone_safe_and_does_not_invent_a_deadline():
    now = datetime(2026, 9, 11, 12, tzinfo=UTC)
    assert proposal_expiry_reason(None, now=now) is None
    assert proposal_expiry_reason(now + timedelta(seconds=1), now=now) is None
    assert proposal_expiry_reason(now, now=now)
    assert proposal_expiry_reason(now.replace(tzinfo=None), now=now)
    assert proposal_expiry_reason(datetime(2026, 9, 10), today=date(2026, 9, 11))
