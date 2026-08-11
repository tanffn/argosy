"""Phase 3c — money surfaces refuse a NON-VALIDATED book (WARN-first, default-off).

The spine gate ships in WARN mode by default: ``load_current_book`` consults the
validated-snapshot predicate, carries a ``validated`` flag on the book, and logs
``would_refuse`` when the head snapshot lacks a PASS integrity verdict — but the
default (``spine_gate_enforce`` False) config changes NO behavior. Enforcement is
a deliberate opt-in (``ARGOSY_SPINE_GATE_ENFORCE=true``) that promotes only the
money-critical surfaces to refuse; read-only displays never refuse.

These tests prove:
  * ``is_snapshot_validated`` — PASS+hash-match True; no verdict / error handling.
  * WARN default — a non-validated book is still returned/resolved (no behavior
    change), only flagged + logged.
  * ENFORCE — the money-critical surfaces (decision-funnel book, numeric
    resolver) refuse; a VALIDATED book proceeds in BOTH modes.
  * The accessor itself never refuses (read-only displays consume it unharmed).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.config import get_settings, reload_settings
from argosy.services.current_book import load_current_book
from argosy.services.decision_funnel.book import load_book
from argosy.services.spine.integrity import record_integrity_verdict_if_absent
from argosy.services.spine.validated_snapshot import (
    is_snapshot_validated,
    read_validated_snapshot,
)
from argosy.state.models import Base, PortfolioSnapshotRow, User


@pytest.fixture
def sync_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'spine.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add(User(id="ariel"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def enforce_on(monkeypatch):
    """Flip ``spine_gate_enforce`` ON via env + settings reload; reset after."""
    monkeypatch.setenv("ARGOSY_SPINE_GATE_ENFORCE", "true")
    reload_settings()
    assert get_settings().spine_gate_enforce is True
    yield
    monkeypatch.delenv("ARGOSY_SPINE_GATE_ENFORCE", raising=False)
    reload_settings()


def _seed_row(session, *, user_id: str = "ariel") -> PortfolioSnapshotRow:
    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=date(2026, 5, 1),
        imported_at=datetime.now(timezone.utc),
        source_path="/tmp/family.tsv",
        positions_json=json.dumps(
            [
                {
                    "location": "Leumi",
                    "currency": "USD",
                    "asset_type": "stock",
                    "symbol": "VWRA",
                    "shares": 10.0,
                    "current_price": 100.0,
                    "current_value_local": 1000.0,
                    "usd_value_k": 5.0,
                }
            ]
        ),
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json="{}",
        fx_usd_nis=3.7,
        fx_usd_eur=4.0,
        parse_warnings_json="[]",
    )
    session.add(row)
    session.commit()
    return row


def _seed_rich(session, *, user_id: str = "ariel") -> PortfolioSnapshotRow:
    """A richer book (VWRA + NVDA) so the NVDA weight/value + estate keys resolve
    on a validated/warn book — the decisive cross-key coverage test needs several
    book-derived keys to actually resolve so we can prove enforce refuses ALL of
    them together (no half-state)."""
    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=date(2026, 5, 1),
        imported_at=datetime.now(timezone.utc),
        source_path="/tmp/family.tsv",
        positions_json=json.dumps(
            [
                {
                    "location": "Leumi", "currency": "USD", "asset_type": "stock",
                    "symbol": "VWRA", "shares": 10.0, "current_price": 100.0,
                    "current_value_local": 1000.0, "usd_value_k": 5.0,
                },
                {
                    "location": "Schwab", "currency": "USD", "asset_type": "stock",
                    "symbol": "NVDA", "shares": 100.0, "current_price": 120.0,
                    "current_value_local": 12000.0, "usd_value_k": 12.0,
                },
            ]
        ),
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json="{}",
        fx_usd_nis=3.7,
        fx_usd_eur=4.0,
        parse_warnings_json="[]",
    )
    session.add(row)
    session.commit()
    return row


def _resolve_book_keys(session, user_id: str = "ariel") -> dict:
    """Run EVERY book-derived resolver site into one ``values`` dict."""
    from argosy.services.plan_numeric_resolver import (
        _apply_nvda_current_weight,
        _apply_nvda_deconcentration,
        _apply_total_net_worth,
        _apply_us_situs_estate,
        _resolve_liquid_net_worth,
        _resolve_net_worth,
        _resolve_usd_exposure,
    )

    values: dict = {}
    for rv in (
        _resolve_net_worth(session, user_id),
        _resolve_liquid_net_worth(session, user_id),
        _resolve_usd_exposure(session, user_id),
    ):
        values[rv.key] = rv
    _apply_total_net_worth(session, user_id, values)
    _apply_us_situs_estate(session, user_id, values)
    _apply_nvda_current_weight(session, user_id, values)
    _apply_nvda_deconcentration(session, user_id, values)
    return values


def _validate(session, row, *, user_id: str = "ariel") -> None:
    """Seed a PASS integrity verdict + head so the book is validated."""
    verdict = record_integrity_verdict_if_absent(session, user_id, row)
    assert verdict is not None and verdict.result == "pass"
    assert read_validated_snapshot(session, user_id, row) is not None


# --------------------------------------------------------------------------
# is_snapshot_validated predicate
# --------------------------------------------------------------------------
def test_predicate_true_on_pass_and_hash_match(sync_session):
    row = _seed_row(sync_session)
    _validate(sync_session, row)
    assert is_snapshot_validated(sync_session, user_id="ariel", snapshot=row) is True


def test_predicate_false_when_no_verdict(sync_session):
    row = _seed_row(sync_session)
    assert is_snapshot_validated(sync_session, user_id="ariel", snapshot=row) is False


def test_predicate_false_on_hash_mismatch(sync_session):
    row = _seed_row(sync_session)
    _validate(sync_session, row)
    # Mutate the committed bytes AFTER assessment → hash no longer matches.
    row.positions_json = json.dumps([{"symbol": "VWRA", "usd_value_k": 999.0}])
    sync_session.commit()
    assert is_snapshot_validated(sync_session, user_id="ariel", snapshot=row) is False


def test_predicate_error_is_best_effort_true_in_warn(sync_session, monkeypatch):
    # Default (warn): an error inside the predicate fails OPEN (True) so it can
    # never break a working money surface.
    assert get_settings().spine_gate_enforce is False
    row = _seed_row(sync_session)
    import argosy.services.spine.validated_snapshot as vs

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(vs, "read_validated_snapshot", _boom)
    assert is_snapshot_validated(sync_session, user_id="ariel", snapshot=row) is True


def test_predicate_error_is_fail_closed_in_enforce(sync_session, enforce_on, monkeypatch):
    row = _seed_row(sync_session)
    import argosy.services.spine.validated_snapshot as vs

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(vs, "read_validated_snapshot", _boom)
    assert is_snapshot_validated(sync_session, user_id="ariel", snapshot=row) is False


# --------------------------------------------------------------------------
# load_current_book — WARN default carries the flag, changes nothing
# --------------------------------------------------------------------------
def test_warn_default_non_validated_book_still_returned(sync_session):
    _seed_row(sync_session)
    assert get_settings().spine_gate_enforce is False
    book = load_current_book(sync_session, "ariel")
    # Flagged, but behavior UNCHANGED: snapshot present, positions resolved.
    assert book.validated is False
    assert book.validation_reason
    assert book.snapshot is not None
    assert book.symbol_usd_k("VWRA") > 0


def test_warn_default_validated_book_flag_true(sync_session):
    row = _seed_row(sync_session)
    _validate(sync_session, row)
    book = load_current_book(sync_session, "ariel")
    assert book.validated is True
    assert book.snapshot is not None


def test_empty_book_is_validated_true_and_pending(sync_session):
    # No snapshot row → empty book, validated True (vacuous), never refuses.
    book = load_current_book(sync_session, "ariel")
    assert book.is_empty is True
    assert book.validated is True


# --------------------------------------------------------------------------
# ENFORCE — money-critical surfaces refuse a non-validated book
# --------------------------------------------------------------------------
def test_enforce_decision_funnel_refuses_non_validated(sync_session, enforce_on):
    _seed_row(sync_session)
    assert load_book(sync_session, user_id="ariel") == []


def test_enforce_decision_funnel_proceeds_on_validated(sync_session, enforce_on):
    row = _seed_row(sync_session)
    _validate(sync_session, row)
    holdings = load_book(sync_session, user_id="ariel")
    assert [h.ticker for h in holdings] == ["VWRA"]


def test_warn_decision_funnel_proceeds_non_validated(sync_session):
    # Same non-validated book as the enforce-refuse case, but WARN default →
    # proceeds. Proves zero behavior change vs today.
    _seed_row(sync_session)
    holdings = load_book(sync_session, user_id="ariel")
    assert [h.ticker for h in holdings] == ["VWRA"]


def test_enforce_resolver_unavailable_non_validated(sync_session, enforce_on):
    from argosy.services.plan_numeric_resolver import _resolve_net_worth

    _seed_row(sync_session)
    rv = _resolve_net_worth(sync_session, "ariel")
    assert rv.status == "unavailable"


def test_enforce_resolver_resolves_validated(sync_session, enforce_on):
    from argosy.services.plan_numeric_resolver import _resolve_net_worth

    row = _seed_row(sync_session)
    _validate(sync_session, row)
    rv = _resolve_net_worth(sync_session, "ariel")
    assert rv.status == "resolved"
    assert rv.value and rv.value > 0


def test_warn_resolver_resolves_non_validated(sync_session):
    # WARN default: the same non-validated book still resolves (no behavior change).
    from argosy.services.plan_numeric_resolver import _resolve_net_worth

    _seed_row(sync_session)
    rv = _resolve_net_worth(sync_session, "ariel")
    assert rv.status == "resolved"


# --------------------------------------------------------------------------
# Read-only displays never refuse, even in enforce
# --------------------------------------------------------------------------
def test_decisive_no_half_state_across_all_book_derived_keys(sync_session, monkeypatch):
    """DECISIVE: with enforce ON + a non-validated book, EVERY book-derived key
    that resolves today refuses together — none stays ``resolved`` while others
    refuse (the half-state Sol reproduced). And a VALIDATED book resolves the
    same set in enforce; WARN default resolves them on the SAME non-validated
    book (zero behavior change)."""
    row = _seed_rich(sync_session)

    # 1. WARN baseline (default) on the NON-validated book: record every key that
    #    resolves today. This is the "no behavior change" reference.
    reload_settings()
    assert get_settings().spine_gate_enforce is False
    # These keys are POLICY CONSTANTS, not book-derived — they legitimately stay
    # resolved regardless of book validation (they never read the snapshot/book).
    _CONSTANT_KEYS = {"concentration.nvda_target_pct"}
    warn_values = _resolve_book_keys(sync_session)
    baseline_resolved = {
        k for k, v in warn_values.items()
        if v.status == "resolved" and k not in _CONSTANT_KEYS
    }
    # Sanity: several book-derived keys genuinely resolve on this book today.
    assert "portfolio.net_worth_nis" in baseline_resolved
    assert "concentration.nvda_value_nis" in baseline_resolved
    assert len(baseline_resolved) >= 4

    # 2. ENFORCE ON, SAME non-validated book: NONE of the baseline-resolved keys
    #    may stay resolved — every one refuses to its degraded shape.
    monkeypatch.setenv("ARGOSY_SPINE_GATE_ENFORCE", "true")
    reload_settings()
    assert get_settings().spine_gate_enforce is True
    enforce_values = _resolve_book_keys(sync_session)
    still_resolved = {
        k for k in baseline_resolved
        if enforce_values.get(k) is not None
        and enforce_values[k].status == "resolved"
    }
    assert still_resolved == set(), f"HALF-STATE — stayed resolved: {still_resolved}"

    # 3. ENFORCE ON, but now VALIDATE the book: the whole baseline resolves again.
    _validate(sync_session, row)
    reload_settings()
    validated_values = _resolve_book_keys(sync_session)
    missing = {
        k for k in baseline_resolved
        if validated_values.get(k) is None
        or validated_values[k].status != "resolved"
    }
    assert missing == set(), f"validated book should resolve all: {missing}"

    monkeypatch.delenv("ARGOSY_SPINE_GATE_ENFORCE", raising=False)
    reload_settings()


def test_accessor_never_refuses_even_in_enforce(sync_session, enforce_on):
    # load_current_book (which every read-only display consumes) still returns the
    # full book in enforce mode — only the money-critical surfaces refuse, on top
    # of the flag. This is what keeps home_greeting/dashboards/target_progress
    # from ever locking out.
    _seed_row(sync_session)
    book = load_current_book(sync_session, "ariel")
    assert book.snapshot is not None
    assert book.validated is False
    assert book.symbol_usd_k("VWRA") > 0
