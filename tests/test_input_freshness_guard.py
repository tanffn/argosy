from datetime import date

from argosy.quality.input_freshness_guard import check_input_freshness


def test_fresh_snapshot_passes():
    v = check_input_freshness(snapshot_as_of=date(2026, 6, 18), today=date(2026, 6, 18))
    assert v.ok and not v.needs_refresh


def test_stale_snapshot_blocks_with_needs_refresh():
    v = check_input_freshness(snapshot_as_of=date(2026, 6, 12), today=date(2026, 6, 25))
    assert v.ok is False and "holdings_snapshot" in v.needs_refresh


def test_low_confidence_input_blocks():
    v = check_input_freshness(
        snapshot_as_of=date(2026, 6, 18), today=date(2026, 6, 18),
        low_confidence_inputs=["savings.annual_net_nis"],
    )
    assert v.ok is False and "savings.annual_net_nis" in v.needs_refresh
