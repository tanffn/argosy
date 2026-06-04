"""Unit tests for the deterministic plan-numeric resolver.

Seeds an in-memory SQLite DB with a portfolio snapshot + per-role
AgentReport rows whose response_text encodes valid typed agent outputs,
then asserts:

  * each ResolvedValue carries the right value, unit, source_locator,
    agent_report_id, and status="resolved";
  * a MISSING role row → that role's keys are status="pending",
    value=None (NO fabricated constant);
  * a MALFORMED response_text → pending, no crash;
  * net worth derives from the snapshot (total_usd_value_k * 1000 * fx).
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.plan_numeric_resolver import (
    ResolvedPlanNumbers,
    ResolvedValue,
    resolve_plan_numbers,
)
from argosy.state.models import (
    AgentReport,
    Base,
    PortfolioSnapshotRow,
    User,
)

DRUN = 71
DECISION_ID = f"plan-synth-{DRUN}"


# ---------------------------------------------------------------------------
# Valid typed payloads (mirror the real agent output JSON shape).
# ---------------------------------------------------------------------------


def _withdrawal_sequencer_json() -> str:
    # required_real_yield = annual_spend / fi_target = 360000 / 8000000 = 0.045
    return json.dumps(
        {
            "fi_bridge": [],
            "withdrawal_schedule": [],
            "fi_base": {
                "fi_target_nis": 8_000_000,
                "retirement_age": 51.7,
                "annual_spend_nis": 360_000,
                "return_assumption_pct": 0.045,
                "required_real_yield_pct": 0.045,
                "method": "annual_spend / required real yield",
            },
            "confidence": "MEDIUM",
            "cited_sources": [],
        }
    )


def _equity_comp_json(
    *, base_avg: float = 500_000.0, others_close: bool = True
) -> str:
    def _scn(name, avg, conf="HIGH"):
        return {
            "name": name,
            "assumptions_md": "x",
            "years": [
                {
                    "year": 2026,
                    "gross_shares": 100.0,
                    "gross_usd": 100000.0,
                    "gross_nis": 345000.0,
                    "net_nis": avg,
                    "net_retention_pct": 47.0,
                    "confidence": "HIGH",
                    "source": "contractual",
                }
            ],
            "five_year_avg_net_nis": avg,
            "fi_date_impact_years": 0.0,
            "confidence": conf,
        }

    other = base_avg if others_close else base_avg * 2.0
    return json.dumps(
        {
            "active_grants": [],
            "scenarios": [
                _scn("known_grants_only", base_avg),
                _scn("conservative_decay", other),
                _scn("optimistic_flat", other),
            ],
            "nvda_sell_on_vest_policy": "defer",
            "advisor_intake_questions": [],
            "confidence": "MEDIUM",
            "cited_sources": [],
        }
    )


def _household_budget_json(*, monthly: float = 23_083.0) -> str:
    return json.dumps(
        {
            "runway_class": "comfortable",
            "monthly_burn_nis": monthly,
            "monthly_income_nis": 40000.0,
            "monthly_net_nis": 16917.0,
            "safe_withdrawal_monthly_usd": 5000.0,
            "headroom_summary": "ok ok.",
            "key_concerns": [],
            "confidence": "HIGH",
            "cited_sources": ["household_budget/identity_yaml"],
        }
    )


def _concentration_json() -> str:
    def _c(name, v):
        return {
            "name": name,
            "value_pct": v,
            "derivation_md": "derivation here",
            "confidence": "MEDIUM",
        }

    return json.dumps(
        {
            "current_nvda_pct": 0.6708,
            "current_risk_contribution_pct": 0.8,
            "tail_loss_p5_1y_pct": 0.3,
            "constraints": [
                _c("sequence_cap", 0.20),
                _c("tail_loss_cap", 0.25),
                _c("risk_contribution_cap", 0.30),
                _c("tax_liquidity_cap", 0.35),
            ],
            "nvda_cap_pct": 0.20,
            "delay_sensitivities": [],
            "sell_down_glidepath_md": "",
            "advisor_intake_questions": [],
            "confidence": "MEDIUM",
            "cited_sources": [],
        }
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'resolver.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.flush()
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_snapshot(s, *, total_usd_k=3_096.0, fx=3.45):
    s.add(
        PortfolioSnapshotRow(
            user_id="ariel",
            imported_at=datetime(2026, 6, 1),
            totals_json=json.dumps({"total_usd_value_k": total_usd_k}),
            fx_usd_nis=fx,
        )
    )
    s.flush()


def _seed_report(s, role: str, response_text: str) -> int:
    row = AgentReport(
        user_id="ariel",
        agent_role=role,
        decision_id=DECISION_ID,
        prompt_hash="h",
        response_text=response_text,
    )
    s.add(row)
    s.flush()
    return row.id


def _seed_all(s):
    _seed_snapshot(s)
    ids = {
        "withdrawal_sequencer": _seed_report(
            s, "withdrawal_sequencer", _withdrawal_sequencer_json()
        ),
        "equity_comp_analyst": _seed_report(
            s, "equity_comp_analyst", _equity_comp_json()
        ),
        "household_budget": _seed_report(
            s, "household_budget", _household_budget_json()
        ),
        "concentration": _seed_report(s, "concentration", _concentration_json()),
    }
    s.commit()
    return ids


# ---------------------------------------------------------------------------
# Tests — fully seeded
# ---------------------------------------------------------------------------


def test_fully_seeded_resolves_every_key(session):
    ids = _seed_all(session)
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    assert isinstance(resolved, ResolvedPlanNumbers)

    # Net worth = 3096 * 1000 * 3.45 = 10,681,200
    nw = resolved.get("portfolio.net_worth_nis")
    assert nw.status == "resolved"
    assert nw.value == pytest.approx(3_096.0 * 1000.0 * 3.45)
    assert nw.unit == "nis"
    assert nw.agent_report_id is None
    assert "total_usd_value_k" in nw.source_locator

    # FI capital target, spend basis, and required yield now come from the
    # DETERMINISTIC fi_methodology (single source of truth), NOT the LLM
    # withdrawal_sequencer agent. With no UserContext seeded here, the
    # methodology is fed the household_budget T12 (23,083*12 = 276,996),
    # adds the amortized life-event params (car 40k + healthcare 15k +
    # home 15k = +70k), and divides the permanent spend by the 3.0% SWR.
    perm_spend = 276_996.0 + 70_000.0  # 346,996
    fi_target = resolved.get("retirement.fi_target_nis")
    assert fi_target.status == "resolved"
    assert fi_target.value == pytest.approx(perm_spend / 0.03, rel=1e-6)
    assert fi_target.source_locator.startswith("fi_methodology")
    assert fi_target.unit == "nis"

    # fi_age is the ONE FI key still derived by the agent (trajectory
    # feasibility) — the methodology does not own it.
    fi_age = resolved.get("retirement.fi_age")
    assert fi_age.value == pytest.approx(51.7)
    assert fi_age.unit == "age"
    assert fi_age.source_locator == "withdrawal_sequencer.fi_base.retirement_age"

    req = resolved.get("retirement.required_real_yield_pct")
    assert req.value == pytest.approx(0.030)  # the defensible perpetual SWR
    assert req.unit == "pct"
    assert req.source_locator.startswith("fi_methodology")

    ret = resolved.get("retirement.return_assumption_pct")
    assert ret.value == pytest.approx(0.05)  # decoupled expected real return

    fi_spend = resolved.get("spend.fi_basis_nis")
    assert fi_spend.value == pytest.approx(perm_spend, rel=1e-6)
    assert fi_spend.source_locator.startswith("fi_methodology")

    reserve = resolved.get("retirement.liquidity_reserve_nis")
    assert reserve.status == "resolved"
    assert reserve.value == pytest.approx(100_000.0)  # wedding buffer only (no edu/mortgage seeded)

    savings = resolved.get("savings.annual_net_nis")
    assert savings.status == "resolved"
    assert savings.value == pytest.approx(500_000.0)
    assert savings.agent_report_id == ids["equity_comp_analyst"]
    assert "known_grants_only" in savings.source_locator

    t12 = resolved.get("spend.annual_t12_nis")
    assert t12.status == "resolved"
    assert t12.value == pytest.approx(23_083.0 * 12)
    assert t12.agent_report_id == ids["household_budget"]

    cap = resolved.get("concentration.nvda_cap_pct")
    assert cap.value == pytest.approx(0.20)
    assert cap.agent_report_id == ids["concentration"]

    cur = resolved.get("concentration.nvda_current_pct")
    assert cur.value == pytest.approx(0.6708)


def test_equity_scenario_disagreement_downgrades_confidence(session):
    _seed_snapshot(session)
    _seed_report(
        session,
        "equity_comp_analyst",
        _equity_comp_json(base_avg=500_000.0, others_close=False),
    )
    session.commit()
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    savings = resolved.get("savings.annual_net_nis")
    # Base scenario value still wins (conservative floor), but confidence
    # is downgraded + the spread is noted in the formula.
    assert savings.value == pytest.approx(500_000.0)
    assert savings.confidence == "LOW"
    assert "spread" in (savings.formula or "")


# ---------------------------------------------------------------------------
# Tests — missing rows → pending, no fabrication
# ---------------------------------------------------------------------------


def test_missing_withdrawal_sequencer_only_drops_fi_age(session):
    # Seed everything EXCEPT withdrawal_sequencer. The FI capital target /
    # spend basis / yield are now DETERMINISTIC (fed by household_budget's
    # T12), so they STILL resolve without the agent — that is the whole point
    # (the headline number no longer depends on a flaky LLM agent). Only
    # fi_age (the agent's trajectory-feasibility number) goes pending.
    _seed_snapshot(session)
    _seed_report(session, "equity_comp_analyst", _equity_comp_json())
    _seed_report(session, "household_budget", _household_budget_json())
    _seed_report(session, "concentration", _concentration_json())
    session.commit()

    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    # Deterministic FI keys still resolve.
    for key in (
        "retirement.fi_target_nis",
        "retirement.required_real_yield_pct",
        "spend.fi_basis_nis",
    ):
        rv = resolved.get(key)
        assert rv.status == "resolved", key
        assert rv.source_locator.startswith("fi_methodology"), key
    # fi_age is agent-owned → pending, never a fabricated constant.
    fi_age = resolved.get("retirement.fi_age")
    assert fi_age.status == "pending"
    assert fi_age.value is None
    # Other roles still resolve.
    assert resolved.get("savings.annual_net_nis").status == "resolved"


def test_fi_target_pending_when_no_spend_basis_at_all(session):
    # No household_budget AND no UserContext → the methodology cannot source
    # a baseline spend → FI target is pending (NEVER a fabricated constant).
    _seed_snapshot(session)
    _seed_report(session, "concentration", _concentration_json())
    session.commit()
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    fi = resolved.get("retirement.fi_target_nis")
    assert fi.status == "pending"
    assert fi.value is None


def test_missing_snapshot_net_worth_pending(session):
    _seed_all(session)
    # Drop the snapshot to simulate a fresh DB with no snapshot.
    session.query(PortfolioSnapshotRow).delete()
    session.commit()
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    nw = resolved.get("portfolio.net_worth_nis")
    assert nw.status == "pending"
    assert nw.value is None


def test_absent_key_returns_pending_sentinel(session):
    _seed_snapshot(session)
    session.commit()
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    # A key that was never produced still returns a typed pending sentinel.
    rv = resolved.get("retirement.fi_target_nis")
    assert rv.status == "pending"
    assert rv.value is None
    assert rv.unit == "nis"


# ---------------------------------------------------------------------------
# Tests — malformed payloads → pending, no crash
# ---------------------------------------------------------------------------


def test_malformed_json_is_pending_no_crash(session):
    _seed_snapshot(session)
    _seed_report(session, "withdrawal_sequencer", "this is { not json")
    session.commit()
    # Must not raise.
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    rv = resolved.get("retirement.fi_target_nis")
    assert rv.status == "pending"
    assert rv.value is None
    assert rv.agent_report_id is not None  # row existed, just unparseable


def test_schema_invalid_payload_is_pending_no_crash(session):
    _seed_snapshot(session)
    # Valid JSON but fi_base missing the required fi_target → model fails.
    bad = json.dumps(
        {"fi_bridge": [], "withdrawal_schedule": [], "confidence": "MEDIUM"}
    )
    _seed_report(session, "withdrawal_sequencer", bad)
    session.commit()
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    rv = resolved.get("retirement.fi_target_nis")
    assert rv.status == "pending"
    assert rv.value is None


def test_fenced_response_text_still_resolves(session):
    # Regression: the ``concentration`` role persists its JSON inside a
    # ```json markdown fence. A bare ``json.loads`` in the resolver choked
    # on the fence and degraded the NVDA cap to pending; the lenient parser
    # (shared with BaseAgent._parse_output) must recover it.
    _seed_snapshot(session)
    fenced = "```json\n" + _concentration_json() + "\n```"
    _seed_report(session, "concentration", fenced)
    session.commit()
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    cap = resolved.get("concentration.nvda_cap_pct")
    assert cap.status == "resolved"
    assert cap.value == pytest.approx(0.20)
    cur = resolved.get("concentration.nvda_current_pct")
    assert cur.status == "resolved"
    assert cur.value == pytest.approx(0.6708)


def test_household_zero_burn_is_pending(session):
    _seed_snapshot(session)
    _seed_report(session, "household_budget", _household_budget_json(monthly=0.0))
    session.commit()
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    rv = resolved.get("spend.annual_t12_nis")
    # Schema default 0.0 must not be reported as a real ₪0/yr household.
    assert rv.status == "pending"
    assert rv.value is None


def test_one_bad_role_does_not_poison_others(session):
    _seed_snapshot(session)
    _seed_report(session, "withdrawal_sequencer", "{bad")
    _seed_report(session, "concentration", _concentration_json())
    session.commit()
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    assert resolved.get("retirement.fi_target_nis").status == "pending"
    assert resolved.get("concentration.nvda_cap_pct").status == "resolved"


def test_fx_usd_nis_resolves_from_boi_cache(session):
    """FX must come from the BOI cache (the authoritative feed), NOT a hardcoded
    3.45. Kills the magic number in the assumption ledger (A5/A6)."""
    from datetime import date as _date
    from decimal import Decimal
    from argosy.state.models import FxRate
    _seed_all(session)
    for d, r in [(_date(2026, 6, 1), Decimal("2.813")), (_date(2026, 6, 2), Decimal("2.84"))]:
        session.add(FxRate(date=d, currency="USD", rate=r, source="boi"))
    session.flush()
    res = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    fx = res.get("fx.usd_nis")
    assert fx is not None and fx.status == "resolved", "fx.usd_nis must resolve from BOI"
    assert 2.5 < float(fx.value) < 3.4, f"BOI-sourced, not 3.45 (got {fx.value})"
    assert "boi" in fx.source_locator.lower()


def test_fx_usd_nis_pending_when_cache_cold(session):
    """No cached BOI rate (cache-only read, no live network in the resolver) →
    pending, never the hardcoded 3.45."""
    _seed_all(session)
    res = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    fx = res.get("fx.usd_nis")
    assert fx is not None and fx.status == "pending"
    assert fx.value is None


def test_net_worth_marks_to_boi_current_fx(session):
    """Net worth = USD assets × CURRENT BOI FX + NIS-native cash (codex FX
    review) — not the erroneous stored snapshot FX. NIS cash kept in native
    shekels, not re-translated as USD exposure."""
    from datetime import date as _date
    from decimal import Decimal
    import json as _json
    from argosy.state.models import FxRate, PortfolioSnapshotRow

    session.add(PortfolioSnapshotRow(
        user_id="ariel", imported_at=datetime(2026, 3, 24),
        snapshot_date=_date(2026, 3, 24), fx_usd_nis=2.94,
        totals_json=_json.dumps({"total_usd_value_k": 1100.0}),
        positions_json=_json.dumps([
            {"symbol": "NVDA", "currency": "USD", "usd_value_k": 1000.0},
            {"symbol": None, "currency": "NIS", "usd_value_k": 100.0},  # native ₪294k @2.94
        ]),
    ))
    session.add(FxRate(date=_date(2026, 6, 2), currency="USD", rate=Decimal("2.80"), source="boi"))
    session.flush()
    res = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    nw = res.get("portfolio.net_worth_nis")
    # USD 1000k × 2.80 + NIS native (100k × 2.94 = ₪294k) = ₪3,094,000
    assert nw.status == "resolved"
    assert abs(float(nw.value) - 3_094_000) < 2_000, f"got {nw.value}"
    assert "2.80" in nw.source_locator or "boi" in nw.source_locator.lower() or "current" in nw.source_locator.lower()
