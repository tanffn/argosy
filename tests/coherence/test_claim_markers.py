# tests/coherence/test_claim_markers.py
from argosy.quality.coherence.claim_markers import (
    render_marker, parse_markers, strip_markers,
)


def test_render_and_parse_roundtrip():
    m = render_marker("retirement_age_headline", {"lead_age": "46", "strict_track_age": "54"})
    text = f"Some prose about retirement. {m}\nMore prose."
    claims = parse_markers(text)
    assert claims["retirement_age_headline"]["lead_age"] == "46"
    assert claims["retirement_age_headline"]["strict_track_age"] == "54"


def test_strip_markers_removes_them_for_human_reading():
    m = render_marker("rsu_vest_policy", {"action": "sell_to_sgov"})
    text = f"Sell net vested NVDA. {m}"
    assert "sell_to_sgov" not in strip_markers(text)
    assert "Sell net vested NVDA." in strip_markers(text)


def test_parse_returns_empty_when_no_markers():
    assert parse_markers("plain prose") == {}
