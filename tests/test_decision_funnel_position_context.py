"""Stage-3 position/flag context fix (SOFI proposal 1, 2026-07-09).

Root incident: Stage 1 routed SOFI as a HELD name on its active
``thesis_monitor_weakened`` flag, but the Stage-3 deep-decision fleet ran with
an empty ``positions_summary`` and no flag context — it answered "should we
initiate SOFI?" and proposed a $3k starter buy while the client already held
~$35.5k of SOFI under a weakened-thesis flag.

Two halves, per the exposure-aware doctrine (deterministic INPUTS, not a
judgment gate):

* ``build_position_context`` — the held position (shares / value / % book /
  account) with explicit TOP-UP framing, plus every active monitor flag on
  the ticker with its reason and an explicit adjudicate-the-conflict
  instruction.
* ``run_deep_decision`` wiring — the block lands in BOTH ``positions_summary``
  (trader packet) and ``user_constraints`` (risk team + fund manager channel),
  without clobbering caller-supplied values or the estate KB block.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.decision_funnel import deep_decision as dd_mod
from argosy.services.decision_funnel.deep_decision import run_deep_decision
from argosy.services.decision_funnel.position_context import build_position_context
from argosy.state.models import (
    Base,
    MonitorFlag,
    PortfolioSnapshotRow,
    PositionStance,
    User,
)


@pytest.fixture
def session():
    eng = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _add_snapshot(session, positions):
    from datetime import date

    # Fresh marks (today): the funnel book now flows through the canonical
    # conserved-book accessor, which degrades a hard-stale/unrepriceable
    # snapshot to an empty book. A current date keeps this fixture exercising
    # the weight/context math rather than the (correct) degrade path.
    session.add(
        PortfolioSnapshotRow(
            user_id="ariel",
            snapshot_date=date.today(),
            imported_at=datetime.now(timezone.utc),
            positions_json=json.dumps(positions),
        )
    )
    session.commit()


def _add_flag(session, *, kind="thesis_monitor_weakened", status="active",
              payload=None, severity="warning"):
    session.add(
        MonitorFlag(
            user_id="ariel",
            kind=kind,
            severity=severity,
            payload=json.dumps(payload or {}),
            surfaced_at=datetime(2026, 6, 14, 18, 20, tzinfo=timezone.utc),
            status=status,
        )
    )
    session.commit()


_SOFI_POSITIONS = [
    {"symbol": "SOFI", "asset_type": "Individual Stocks", "usd_value_k": 35.46,
     "shares": 2000.0, "location": "Leumi"},
    {"symbol": "NVDA", "asset_type": "Individual Stocks", "usd_value_k": 2000.0,
     "shares": 10000.0, "location": "Schwab"},
    {"symbol": "CSPX", "asset_type": "Core Equity", "usd_value_k": 1500.0,
     "shares": 155.0, "location": "Leumi"},
    {"symbol": "-", "asset_type": "Cash", "usd_value_k": 29.0},
]


# ---------------------------------------------------------------------------
# build_position_context — the block itself
# ---------------------------------------------------------------------------


def test_held_position_renders_shares_value_account_and_topup_framing(session):
    _add_snapshot(session, _SOFI_POSITIONS)
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "CURRENT POSITION — SOFI" in block
    assert "2,000 shares" in block
    assert "$35,460" in block
    assert "Leumi" in block
    # % of the tradeable book (same definition Stage 1 routes on):
    # 35.46 / (35.46 + 2000 + 1500) ≈ 1.00%
    assert "1.00% of the tradeable securities book" in block
    assert "ALREADY OWNS SOFI" in block
    assert "TOP-UP" in block
    assert "NEVER frame" in block


def test_not_held_renders_initiation_framing(session):
    _add_snapshot(session, _SOFI_POSITIONS)
    block = build_position_context(session, user_id="ariel", ticker="PLTR")
    assert "CURRENT POSITION — PLTR: NOT HELD" in block
    assert "INITIATE a new position" in block
    assert "TOP-UP" not in block


def test_active_flag_included_with_reason_and_adjudication_instruction(session):
    _add_snapshot(session, _SOFI_POSITIONS)
    _add_flag(session, payload={
        "ticker": "SOFI", "thesis_status": "weakened",
        "rationale_md": "Feed flags non-trivial pressure: legal headwinds.",
    })
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "ACTIVE MONITOR FLAGS — SOFI:" in block
    assert "thesis_monitor_weakened" in block
    assert "severity=warning" in block
    assert "surfaced 2026-06-14" in block
    assert "thesis_status=weakened" in block
    assert "legal headwinds" in block
    # The fleet must adjudicate buy-more-vs-flag explicitly.
    assert "EXPLICITLY adjudicate" in block


def test_non_active_and_other_ticker_flags_excluded(session):
    _add_snapshot(session, _SOFI_POSITIONS)
    _add_flag(session, status="superseded",
              payload={"ticker": "SOFI", "rationale_md": "old"})
    _add_flag(session, status="active",
              payload={"ticker": "NVDA", "rationale_md": "other name"})
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "ACTIVE MONITOR FLAGS" not in block


def test_flag_matched_via_primary_field_holding_prefix(session):
    _add_snapshot(session, _SOFI_POSITIONS)
    _add_flag(session, payload={
        "primary_field": "holding.SOFI", "rationale_md": "watchlist-level event",
    })
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "thesis_monitor_weakened" in block
    assert "watchlist-level event" in block


def test_long_flag_rationale_is_excerpted(session):
    _add_snapshot(session, _SOFI_POSITIONS)
    _add_flag(session, payload={"ticker": "SOFI", "rationale_md": "x" * 2000})
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "x" * 700 in block
    assert "x" * 701 not in block


def test_no_snapshot_degrades_to_not_held(session):
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "NOT HELD" in block


def test_multi_account_positions_are_all_listed_and_summed(session):
    _add_snapshot(session, _SOFI_POSITIONS + [
        {"symbol": "SOFI", "asset_type": "Individual Stocks", "usd_value_k": 10.0,
         "shares": 550.0, "location": "IBKR"},
    ])
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "Leumi" in block
    assert "IBKR" in block
    assert "$45,460 total" in block


# ---------------------------------------------------------------------------
# STANDING PLAN STANCE block (one-voice reconciliation, NVDA verdict-34 fix)
# ---------------------------------------------------------------------------


def _add_stance(session, *, symbol, stance, source="plan", conviction="LOW",
                divergence=False):
    """Seed a PositionStance row that get_stances will serve WITHOUT rebuilding.

    A rebuild would delete-and-reinsert from the plan layer (and drop our seed
    when there's no plan). We force get_stances down the "serve stored rows"
    path by making the stance non-stale: built now, plan_version_id/snapshot_key
    matching the no-plan / no-snapshot fingerprint the freshness check computes.
    """
    session.add(
        PositionStance(
            user_id="ariel",
            symbol=symbol,
            stance=stance,
            stance_source=source,
            conviction=conviction,
            plan_verdict=stance,
            divergence=divergence,
            reasoning_md="",
            plan_version_id=None,
            snapshot_key="None|0|0.0",
            built_at=datetime.now(timezone.utc),
        )
    )
    session.commit()


def _freeze_stance_sources(monkeypatch):
    """Neutralize the plan/snapshot freshness inputs so the seeded stance row
    is served as-is (no rebuild)."""
    import argosy.services.position_stance as ps_mod

    monkeypatch.setattr(ps_mod, "_load_plan_version", lambda db, uid: None)
    monkeypatch.setattr(ps_mod, "_load_portfolio_snapshot", lambda uid, db=None: None)


def test_standing_sell_stance_renders_reconcile_block(session, monkeypatch):
    _freeze_stance_sources(monkeypatch)
    _add_snapshot(session, _SOFI_POSITIONS)
    _add_stance(session, symbol="NVDA", stance="SELL", source="plan",
                conviction="LOW")
    block = build_position_context(session, user_id="ariel", ticker="NVDA")
    assert "STANDING PLAN STANCE (one-voice, authoritative): SELL" in block
    assert "source=plan" in block
    assert "conviction=LOW" in block
    assert "active plan trim/deconcentration pace" in block
    assert "MIRROR" in block
    assert "PROPOSED STANCE REVISION:" in block
    assert "bare HOLD" in block


def test_standing_trim_stance_renders_reconcile_block(session, monkeypatch):
    _freeze_stance_sources(monkeypatch)
    _add_snapshot(session, _SOFI_POSITIONS)
    _add_stance(session, symbol="SOFI", stance="TRIM", source="review",
                conviction="MED")
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "STANDING PLAN STANCE (one-voice, authoritative): TRIM" in block
    assert "standing TRIM" in block


def test_standing_hold_stance_has_no_reconcile_mandate(session, monkeypatch):
    _freeze_stance_sources(monkeypatch)
    _add_snapshot(session, _SOFI_POSITIONS)
    _add_stance(session, symbol="SOFI", stance="HOLD", source="plan")
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "STANDING PLAN STANCE (one-voice, authoritative): HOLD" in block
    # A HOLD stance imposes no mirror-or-propose mandate.
    assert "active plan trim/deconcentration pace" not in block
    assert "PROPOSED STANCE REVISION:" not in block


def test_divergence_flag_rendered(session, monkeypatch):
    _freeze_stance_sources(monkeypatch)
    _add_snapshot(session, _SOFI_POSITIONS)
    _add_stance(session, symbol="NVDA", stance="SELL", divergence=True)
    block = build_position_context(session, user_id="ariel", ticker="NVDA")
    assert "DIVERGENCE FLAGGED" in block


def test_no_stance_row_omits_block(session, monkeypatch):
    _freeze_stance_sources(monkeypatch)
    _add_snapshot(session, _SOFI_POSITIONS)
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "STANDING PLAN STANCE" not in block


def test_stance_read_failure_never_raises(session, monkeypatch):
    import argosy.services.position_stance as ps_mod

    def _boom(db, user_id, *a, **k):
        raise RuntimeError("stance registry unavailable")

    monkeypatch.setattr(ps_mod, "get_stances", _boom)
    _add_snapshot(session, _SOFI_POSITIONS)
    # Best-effort: the block still builds (position lines) with no stance section.
    block = build_position_context(session, user_id="ariel", ticker="SOFI")
    assert "CURRENT POSITION — SOFI" in block
    assert "STANDING PLAN STANCE" not in block


# ---------------------------------------------------------------------------
# run_deep_decision wiring — the block reaches the fleet packet
# ---------------------------------------------------------------------------


def _stub_fleet(monkeypatch, captured: dict):
    async def _open(**kwargs):
        return 7

    async def _analysts(**kwargs):
        return SimpleNamespace(reports=[])

    class _FakeFlow:
        def __init__(self, *, user_id):
            self.user_id = user_id

        async def run(self, **kwargs):
            captured.update(kwargs)
            from argosy.decisions.flow import BlockedProposal

            return BlockedProposal(
                reason="test stop", blocked_by="fund_manager", decision_run_id=7
            )

    monkeypatch.setattr(dd_mod, "open_decision_run_for_consult", _open)
    monkeypatch.setattr(dd_mod, "run_per_ticker_analysts", _analysts)
    monkeypatch.setattr(dd_mod, "DecisionFlow", _FakeFlow)
    # Isolate the pushback gate from the LIVE verdict registry: without this the
    # gate reads a real settled verdict for the ticker (e.g. SOFI defended from
    # prior fleet runs) and short-circuits BEFORE the stage-3 packet is built,
    # so flow.run() is never called and the captured kwargs are empty. These
    # tests exercise PACKET BUILDING, not the gate (which has its own tests);
    # force "not defended". It's imported locally inside run_deep_decision, so
    # patch the source module.
    import argosy.services.verdict_registry as _vr_mod

    monkeypatch.setattr(
        _vr_mod, "check_pushback_gate",
        lambda *a, **k: SimpleNamespace(defended=False, standing=None, reason=""),
    )


@pytest.mark.asyncio
async def test_stage3_packet_carries_position_context(monkeypatch) -> None:
    """The context block lands in positions_summary AND user_constraints
    (trader reads the former; risk team + FM read the latter)."""
    captured: dict = {}
    _stub_fleet(monkeypatch, captured)

    sentinel = (
        "CURRENT POSITION — SOFI (latest portfolio snapshot):\n"
        "- HELD: 2,000 shares, ~$35,460 in Leumi.\n"
        "ACTIVE MONITOR FLAGS — SOFI:\n- thesis_monitor_weakened"
    )

    async def _ctx(**kwargs):
        assert kwargs == {"user_id": "ariel", "ticker": "SOFI"}
        return sentinel

    monkeypatch.setattr(dd_mod, "position_context_block", _ctx)

    out = await run_deep_decision(user_id="ariel", ticker="SOFI")
    assert out.status == "blocked"
    assert captured["positions_summary"] == sentinel
    assert sentinel in captured["user_constraints"]
    # The estate KB block must still be present (both INPUTS fixes coexist).
    assert "domain_knowledge/tax/us/estate_tax_nonresidents.md" in (
        captured["user_constraints"]
    )


@pytest.mark.asyncio
async def test_caller_supplied_positions_summary_is_preserved(monkeypatch) -> None:
    captured: dict = {}
    _stub_fleet(monkeypatch, captured)

    async def _ctx(**kwargs):
        return "POSITION CONTEXT BLOCK"

    monkeypatch.setattr(dd_mod, "position_context_block", _ctx)

    await run_deep_decision(
        user_id="ariel", ticker="SOFI",
        positions_summary="caller positions", user_constraints="keep me",
    )
    # Caller's summary wins; the context still reaches user_constraints,
    # after the caller's own constraints.
    assert captured["positions_summary"] == "caller positions"
    assert captured["user_constraints"].startswith("keep me")
    assert "POSITION CONTEXT BLOCK" in captured["user_constraints"]


@pytest.mark.asyncio
async def test_position_context_failure_never_kills_stage3(monkeypatch) -> None:
    captured: dict = {}
    _stub_fleet(monkeypatch, captured)

    async def _boom(**kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(dd_mod, "position_context_block", _boom)

    out = await run_deep_decision(user_id="ariel", ticker="SOFI")
    assert out.status == "blocked"  # the flow still ran
    assert captured["positions_summary"] == ""
