"""Ingest durability — the half that keeps a repaired book repaired.

Two failure classes, both observed in the $2.4M incident's blast radius:

1. ``latest_matches_snapshot`` was content-blind, so a corrected re-export of
   the same file on the same day was mistaken for a no-op and silently dropped.
2. ``SnapshotIngestRejected`` was swallowed by broad ``except Exception``
   handlers, so a feed that would erase an account failed quietly — and in the
   plan-synthesis path the rejected feed was still used to build inputs.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
from argosy.services.portfolio_snapshot_store import (
    feed_content_digest,
    latest_matches_snapshot,
    persist_snapshot,
)
from argosy.state.models import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    s = maker()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _pos(symbol: str, location: str, shares: float, value_k: float,
         price: float | None = None) -> PortfolioPosition:
    return PortfolioPosition(
        symbol=symbol,
        location=location,
        shares=shares,
        usd_value_k=value_k,
        current_price=price,
    )


def _snap(positions: list[PortfolioPosition], *, source: str = "feed.tsv",
          when: date = date(2026, 8, 8)) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_date=when,
        source_path=source,
        positions=positions,
        allocations=[],
        nvda_sales=[],
        real_estate=[],
        pensions=[],
        fx_usd_nis=3.35,
        fx_usd_eur=0.92,
    )


TWO_ACCOUNTS = [
    _pos("NVDA", "schwab", 10940.0, 2307.9, 210.9),
    _pos("CSPX", "leumi", 100.0, 60.0, 600.0),
]


def test_identical_reimport_is_still_a_noop(session):
    """Idempotency must survive the stricter check, or we bloat the table."""
    snap = _snap(TWO_ACCOUNTS)
    persist_snapshot(session, user_id="ariel", snapshot=snap)
    assert latest_matches_snapshot(session, user_id="ariel", snapshot=snap) is True


def test_changed_values_are_not_mistaken_for_a_noop(session):
    """Same path, same date, same COUNT — but corrected numbers.

    Revert detector: compare only source_path + date + position count → this
    fails, because the re-export looks identical and the change is dropped.
    """
    persist_snapshot(session, user_id="ariel", snapshot=_snap(TWO_ACCOUNTS))

    corrected = [
        _pos("NVDA", "schwab", 10940.0, 2280.4, 208.4),  # repriced
        _pos("CSPX", "leumi", 100.0, 60.0, 600.0),
    ]
    assert len(corrected) == len(TWO_ACCOUNTS)
    assert latest_matches_snapshot(
        session, user_id="ariel", snapshot=_snap(corrected)
    ) is False


def test_changed_share_count_is_not_a_noop(session):
    """A buy/sell of the same symbol keeps the count identical."""
    persist_snapshot(session, user_id="ariel", snapshot=_snap(TWO_ACCOUNTS))
    traded = [
        _pos("NVDA", "schwab", 9940.0, 2307.9, 210.9),  # 1,000 shares sold
        _pos("CSPX", "leumi", 100.0, 60.0, 600.0),
    ]
    assert latest_matches_snapshot(
        session, user_id="ariel", snapshot=_snap(traded)
    ) is False


def test_symbol_substitution_is_not_a_noop(session):
    """Same count and same total value, different instruments entirely."""
    persist_snapshot(session, user_id="ariel", snapshot=_snap(TWO_ACCOUNTS))
    swapped = [
        _pos("AMD", "schwab", 10940.0, 2307.9, 210.9),
        _pos("CSPX", "leumi", 100.0, 60.0, 600.0),
    ]
    assert latest_matches_snapshot(
        session, user_id="ariel", snapshot=_snap(swapped)
    ) is False


def test_row_without_a_digest_is_never_a_match(session):
    """Legacy / restore-written rows must err toward a guarded write.

    Returning True there would let the first post-repair feed be skipped.
    """
    persist_snapshot(session, user_id="ariel", snapshot=_snap(TWO_ACCOUNTS))
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row

    row = get_latest_snapshot_row(session, "ariel")
    totals = json.loads(row.totals_json)
    del totals["feed_row_hash"]
    row.totals_json = json.dumps(totals)
    session.commit()

    assert latest_matches_snapshot(
        session, user_id="ariel", snapshot=_snap(TWO_ACCOUNTS)
    ) is False


def test_digest_ignores_row_order(session):
    """Feed row order is not economic content; reordering must stay a no-op."""
    a = feed_content_digest(TWO_ACCOUNTS)
    b = feed_content_digest(list(reversed(TWO_ACCOUNTS)))
    assert a == b


def test_changed_fx_is_not_a_noop(session):
    """FX rates are persisted economics — a change must not be dropped.

    Revert detector: digest only the five position fields → this fails, because
    positions are identical and only the FX rates moved.
    """
    persist_snapshot(session, user_id="ariel", snapshot=_snap(TWO_ACCOUNTS))
    revalued = _snap(TWO_ACCOUNTS)
    revalued.fx_usd_nis = 3.71
    assert latest_matches_snapshot(
        session, user_id="ariel", snapshot=revalued
    ) is False


def test_changed_position_currency_is_not_a_noop(session):
    """Fields outside the original five still change the economics."""
    persist_snapshot(session, user_id="ariel", snapshot=_snap(TWO_ACCOUNTS))
    reclassified = [dict(p.model_dump()) for p in TWO_ACCOUNTS]
    reclassified[0]["currency"] = "ILS"
    snap = _snap(TWO_ACCOUNTS)
    snap.positions = [PortfolioPosition(**d) for d in reclassified]
    assert latest_matches_snapshot(
        session, user_id="ariel", snapshot=snap
    ) is False


def test_partial_feed_carries_the_uncovered_account_instead_of_erasing_it(session):
    """The Jul-13 incident, as a test: a Leumi-only feed must not erase schwab.

    Note this is a no-erasure assertion rather than a rejection. The per-account
    merge carries uncovered accounts forward, so a partial feed is no longer
    destructive and therefore has nothing to refuse — the rejection path exists
    for feeds that claim an account and then drop its positions.
    """
    persist_snapshot(session, user_id="ariel", snapshot=_snap(TWO_ACCOUNTS))
    leumi_only = _snap(
        [_pos("CSPX", "leumi", 100.0, 60.0, 600.0)], source="leumi_only.tsv",
    )
    row = persist_snapshot(session, user_id="ariel", snapshot=leumi_only)

    positions = json.loads(row.positions_json)
    symbols = {str(p.get("symbol") or "").upper() for p in positions}
    accounts = {str(p.get("location") or "").lower() for p in positions}
    assert "NVDA" in symbols, "the uncovered schwab account was ERASED"
    assert accounts == {"leumi", "schwab"}

    totals = json.loads(row.totals_json)
    carried = [str(a).lower() for a in totals.get("accounts_carried") or []]
    assert "schwab" in carried, f"schwab should be recorded as carried: {totals}"


def test_pair_write_through_surfaces_rejection_to_the_operator(monkeypatch, tmp_path):
    """The XLS/OSH pair path must report the rejection, not log-and-forget.

    Revert detector: remove the ``except SnapshotIngestRejected`` clause so the
    broad handler swallows it → this fails (returns [] with no explanation).
    """
    from argosy.services.holding_books import SnapshotIngestRejected
    from argosy.services.portfolio_ingest import xls_osh_pair

    tsv = tmp_path / "leumi_only.tsv"
    tsv.write_text("dummy", encoding="utf-8")

    monkeypatch.setattr(
        "argosy.ingest.tsv.parse_portfolio_tsv",
        lambda *a, **k: _snap([_pos("CSPX", "leumi", 100.0, 60.0)]),
    )

    def _reject(*_a, **_k):
        raise SnapshotIngestRejected(
            "account_erasure",
            "would remove accounts: schwab, aborad (3 positions, $2432.0k)",
        )

    monkeypatch.setattr(
        "argosy.services.portfolio_snapshot_store.write_through_if_changed",
        _reject,
    )

    lines = xls_osh_pair._write_through_resolved_snapshot(  # noqa: SLF001
        db=None, user_id="ariel", tsv_path=tsv, commit=False,
    )

    assert lines, "a rejected feed must produce an operator-visible line"
    joined = " ".join(lines)
    assert "SNAPSHOT_INGEST_REJECTED" in joined
    assert "NOT updated" in joined
    assert "schwab" in joined, "the message must name what would be erased"


def test_plan_synthesis_does_not_build_inputs_from_a_rejected_feed(
    monkeypatch, session, tmp_path,
):
    """A feed the store refused must not reach plan synthesis either.

    The DB guard protected the book while the planner kept consuming the
    truncated feed from memory, which is how a plan gets synthesised against a
    book missing 59% of its value.

    Revert detector: return the parsed snapshot instead of None in the
    ``SnapshotIngestRejected`` branch → this fails.
    """
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod
    from argosy.services.holding_books import SnapshotIngestRejected

    tsv = tmp_path / "leumi_only.tsv"
    tsv.write_text("dummy", encoding="utf-8")

    truncated = _snap([_pos("CSPX", "leumi", 100.0, 60.0)])
    monkeypatch.setattr(inputs_mod, "_find_latest_tsv", lambda: tsv)
    monkeypatch.setattr(
        "argosy.ingest.tsv.parse_portfolio_tsv", lambda *a, **k: truncated,
    )

    def _reject(*_a, **_k):
        raise SnapshotIngestRejected(
            "account_erasure", "would remove accounts: schwab, aborad",
        )

    monkeypatch.setattr(
        "argosy.services.portfolio_snapshot_store.write_through_if_changed",
        _reject,
    )

    class _Log:
        def __init__(self):
            self.errors: list[tuple] = []

        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, event, **k):
            self.errors.append((event, k))

    log = _Log()
    resolved = inputs_mod._resolve_snapshot_for_inputs(  # noqa: SLF001
        session=session, user_id="ariel", log=log,
    )

    assert resolved is None, "a rejected feed must not be used for synthesis"
    assert any(
        e[0] == "plan_synthesis.inputs.snapshot_rejected_not_used"
        for e in log.errors
    ), f"the rejection must be logged loudly, got: {log.errors}"


def test_nvda_trajectory_reports_the_book_not_the_file_on_disk(
    monkeypatch, session, tmp_path,
):
    """A user-visible share count must come from the persisted book.

    The route parsed the newest TSV off disk, so a feed the store had REFUSED
    for erasing accounts still set `today_shares` — and NVDA is exactly the
    position such a feed erases. A review demonstrated `today_shares=999` from
    a rejected feed.

    Revert detector: read NVDA from `parse_portfolio_tsv` again → this fails,
    because the route reports 999 instead of the book's 10,940.
    """
    from argosy.api.routes import plan as plan_routes

    # The book holds the truth: NVDA 10,940 shares.
    persist_snapshot(
        session,
        user_id="ariel",
        snapshot=_snap([_pos("NVDA", "schwab", 10940.0, 2307.9)]),
    )

    # A rejected export on disk claims something else entirely.
    tsv = tmp_path / "rejected.tsv"
    tsv.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(
        "argosy.ingest.tsv.parse_portfolio_tsv",
        lambda *a, **k: _snap([_pos("NVDA", "leumi", 999.0, 200.0)]),
    )

    resp = plan_routes._compute_nvda_trajectory(  # noqa: SLF001
        user_id="ariel", db=session, tsv=tsv,
    )

    assert resp.today_shares != 999, (
        "the rejected file on disk must not set a user-visible share count"
    )
    assert resp.today_shares == 10940, (
        f"expected the book's NVDA position, got {resp.today_shares}"
    )


def test_portfolio_summary_does_not_reparse_a_rejected_feed(
    monkeypatch, session, tmp_path,
):
    """The Phase-3 summary helper must not bypass the ingest guard.

    `_assemble_portfolio_summary` parsed the latest TSV straight off disk, so a
    feed the store had REFUSED still reached Synthesizer Phase 3 through this
    second door — the database was protected while the plan was written against
    the truncated book.

    Revert detector: parse the TSV directly in that helper again → this fails,
    because the rejected symbol reappears in the summary text.
    """
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod
    from argosy.services.holding_books import SnapshotIngestRejected

    tsv = tmp_path / "leumi_only.tsv"
    tsv.write_text("dummy", encoding="utf-8")

    # The rejected feed contains ONLY this symbol; it must not appear.
    rejected = _snap([_pos("REJECTEDSYM", "leumi", 100.0, 60.0)])
    monkeypatch.setattr(inputs_mod, "_find_latest_tsv", lambda: tsv)
    monkeypatch.setattr(
        "argosy.ingest.tsv.parse_portfolio_tsv", lambda *a, **k: rejected,
    )

    def _reject(*_a, **_k):
        raise SnapshotIngestRejected(
            "account_erasure", "would remove accounts: schwab, aborad",
        )

    monkeypatch.setattr(
        "argosy.services.portfolio_snapshot_store.write_through_if_changed",
        _reject,
    )

    summary = inputs_mod._assemble_portfolio_summary(  # noqa: SLF001
        session=session, user_id="ariel",
    )

    assert "REJECTEDSYM" not in summary, (
        f"the rejected feed leaked into the Phase-3 summary: {summary!r}"
    )
    assert "REJECTED" in summary or "no positions" in summary, (
        f"the summary must say the data is unavailable, got: {summary!r}"
    )
