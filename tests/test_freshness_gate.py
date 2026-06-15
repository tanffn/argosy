from datetime import date

from argosy.quality.freshness_gate import check_input_freshness
from argosy.quality.gate_types import GateCheck


def test_stale_snapshot_is_flagged():
    """Snapshot older than the freshness window vs `today` is a currency defect —
    the system must distrust its own stored state (the pre-sale-book class)."""
    viol = check_input_freshness(
        today=date(2026, 6, 15),
        snapshot_date=date(2026, 6, 12),
        analyst_report_dates={"macro": date(2026, 6, 14)},
        max_snapshot_age_days=2,  # 06-12 -> 3 days old > 2
        max_report_age_days=2,
    )
    assert any(v.check is GateCheck.INPUT_FRESHNESS and "snapshot" in v.detail for v in viol)


def test_fresh_inputs_pass():
    viol = check_input_freshness(
        today=date(2026, 6, 15), snapshot_date=date(2026, 6, 15),
        analyst_report_dates={"macro": date(2026, 6, 15)},
        max_snapshot_age_days=2, max_report_age_days=2,
    )
    assert viol == []
