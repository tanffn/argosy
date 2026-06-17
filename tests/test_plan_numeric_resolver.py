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
    render_numbers_for_synth,
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


def test_fi_margin_signed_is_single_sourced(session):
    """The FI sufficiency margin must be ONE signed value (net_worth − FI-total)
    so every surface cites the same number with the same sign. The
    'reached vs −118,020 not-reached' contradiction was two surfaces computing
    the margin independently with opposite sign conventions."""
    _seed_all(session)
    res = resolve_plan_numbers(
        session, user_id="ariel", decision_run_id=DRUN, include_canonical_ages=False
    )
    nw = res.get("portfolio.net_worth_nis")
    tot = res.get("retirement.fi_total_capital_nis")
    margin = res.get("retirement.fi_margin_signed_nis")
    assert margin is not None and margin.status == "resolved"
    # margin = net_worth − fi_total; positive => total target reached.
    assert abs(float(margin.value) - (float(nw.value) - float(tot.value))) < 1.0
    assert "net_worth" in margin.formula and "fi_total" in margin.formula


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


# ---------------------------------------------------------------------------
# Tests — canonical dual-track retirement ages (opt-in; recursion-safe)
#
# The /retirement headline, the ruin hero, and (after this) the /plan narrative
# + synthesizer all bind to the ONE canonical dual-track age from
# retirement_plan.canonical_feasible_dual_track. The resolver exposes it only
# when a DISPLAY surface opts in (include_canonical_ages=True). The default is
# False so the ~8 non-display callers stay cheap AND the re-entrant
# _nvda_deconcentration_haircut -> resolve_plan_numbers hop (reached from
# canonical_feasible_dual_track itself) cannot infinite-recurse.
# ---------------------------------------------------------------------------


def _fake_feasible(*, earliest=46.0, p=0.91, pres=53.0):
    from argosy.services.retirement.scenario_mc import FeasibleAgeResult

    return FeasibleAgeResult(
        earliest_feasible_age=earliest,
        p_solvent_at_age=p,
        target_p_solvent=0.90,
        operational_target_age=49.0,
        statutory_lump_age=60,
        statutory_annuity_age=67,
        current_age=43.96,
        reserve_netted_nis=0.0,
        basis={
            "preservation_age": pres,
            "source": "retirement_plan.canonical_feasible_dual_track",
        },
    )


def test_canonical_ages_resolve_when_opted_in(session, monkeypatch):
    """include_canonical_ages=True exposes the canonical dual-track earliest-safe
    (typical drawdown) age + the capital-preservation age, sourced from
    retirement_plan.canonical_feasible_dual_track — NOT the stale fi_age."""
    import argosy.services.retirement.retirement_plan as rp

    monkeypatch.setattr(rp, "canonical_feasible_dual_track", lambda **kw: _fake_feasible())
    _seed_all(session)
    resolved = resolve_plan_numbers(
        session, user_id="ariel", decision_run_id=DRUN, include_canonical_ages=True
    )

    early = resolved.get("retirement.earliest_safe_age")
    assert early.status == "resolved"
    assert early.value == pytest.approx(46.0)
    assert early.unit == "age"
    assert (
        early.source_locator
        == "retirement_plan.canonical_feasible_dual_track.earliest_feasible_age"
    )

    pres = resolved.get("retirement.preservation_age")
    assert pres.status == "resolved"
    assert pres.value == pytest.approx(53.0)
    assert pres.unit == "age"
    assert "preservation_age" in pres.source_locator

    # fi_age (the trajectory-feasibility number) is untouched — still its own
    # value, kept for FIRE-bridge sizing.
    fi_age = resolved.get("retirement.fi_age")
    assert fi_age.value == pytest.approx(51.7)


def test_canonical_ages_not_computed_by_default(session, monkeypatch):
    """Default (no opt-in) must NOT invoke the heavy canonical MC. This is what
    keeps the re-entrant _nvda_deconcentration_haircut -> resolve_plan_numbers
    path from infinite-recursing, and keeps the non-display callers cheap. The
    age keys stay pending (never fabricated)."""
    import argosy.services.retirement.retirement_plan as rp

    calls = {"n": 0}

    def _spy(**kw):
        calls["n"] += 1
        return _fake_feasible()

    monkeypatch.setattr(rp, "canonical_feasible_dual_track", _spy)
    _seed_all(session)
    resolved = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)

    assert calls["n"] == 0, "canonical_feasible_dual_track must not run by default"
    assert resolved.get("retirement.earliest_safe_age").status == "pending"
    assert resolved.get("retirement.earliest_safe_age").value is None
    assert resolved.get("retirement.preservation_age").status == "pending"


def test_canonical_age_failure_is_pending_no_fabrication(session, monkeypatch):
    """If the canonical engine raises (thin data, MC error), the earliest-safe
    age degrades to pending — NEVER a fabricated or stale fallback number
    (output-trust doctrine: every number Argosy-derived or absent)."""
    import argosy.services.retirement.retirement_plan as rp

    def _boom(**kw):
        raise RuntimeError("MC blew up")

    monkeypatch.setattr(rp, "canonical_feasible_dual_track", _boom)
    _seed_all(session)
    resolved = resolve_plan_numbers(
        session, user_id="ariel", decision_run_id=DRUN, include_canonical_ages=True
    )
    early = resolved.get("retirement.earliest_safe_age")
    assert early.status == "pending"
    assert early.value is None


def test_canonical_age_none_when_no_safe_age(session, monkeypatch):
    """A portfolio that never clears the solvency bar (earliest_feasible_age is
    None) resolves the earliest-safe age to pending, not 0 or a guess."""
    import argosy.services.retirement.retirement_plan as rp

    monkeypatch.setattr(
        rp, "canonical_feasible_dual_track", lambda **kw: _fake_feasible(earliest=None)
    )
    _seed_all(session)
    resolved = resolve_plan_numbers(
        session, user_id="ariel", decision_run_id=DRUN, include_canonical_ages=True
    )
    assert resolved.get("retirement.earliest_safe_age").status == "pending"


def test_render_synth_leads_with_earliest_safe_age(session, monkeypatch):
    """The synth/narrative numbers block surfaces the canonical earliest-safe age
    BEFORE the fi_age line, and relabels fi_age as the full-FI/perpetuity target
    (so the narrative can no longer call 49 'the earliest you can retire')."""
    import argosy.services.retirement.retirement_plan as rp

    monkeypatch.setattr(rp, "canonical_feasible_dual_track", lambda **kw: _fake_feasible())
    _seed_all(session)
    resolved = resolve_plan_numbers(
        session, user_id="ariel", decision_run_id=DRUN, include_canonical_ages=True
    )
    block = render_numbers_for_synth(resolved)

    assert "age 46.0" in block  # the honest earliest-safe age is stated
    assert "age 53.0" in block  # the preservation what-if
    # fi_age must no longer be labeled as the "earliest" age.
    assert "Earliest feasible FI age" not in block
    # earliest-safe age leads the fi_age (49/51.7) line.
    assert block.index("age 46.0") < block.index("age 51.7")


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


def test_usd_exposure_sums_usd_positions_at_current_boi_fx(session):
    """portfolio.usd_exposure_nis = NIS value of USD-denominated assets ONLY
    (codex FX-shock review): USD positions × current BOI FX, EXCLUDING NIS-native
    holdings. This is the FX-shock base; it must be larger than the US-situs
    estate figure when there is USD exposure beyond US-situs securities, and it
    must NOT include NIS-native cash."""
    from datetime import date as _date
    from decimal import Decimal
    import json as _json
    from argosy.state.models import FxRate, PortfolioSnapshotRow

    session.add(PortfolioSnapshotRow(
        user_id="ariel", imported_at=datetime(2026, 6, 1),
        snapshot_date=_date(2026, 6, 1), fx_usd_nis=2.94,  # stale stored fx
        totals_json=_json.dumps({"total_usd_value_k": 1300.0}),
        positions_json=_json.dumps([
            {"symbol": "NVDA", "currency": "USD", "usd_value_k": 1000.0},
            {"symbol": "VWRA", "currency": "USD", "usd_value_k": 300.0},  # Irish UCITS, USD-exposed
            {"symbol": None, "currency": "NIS", "usd_value_k": 200.0},    # NIS-native — excluded
        ]),
    ))
    session.add(FxRate(date=_date.today(), currency="USD", rate=Decimal("3.10"), source="boi"))
    session.flush()
    res = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    usd_exp = res.get("portfolio.usd_exposure_nis")
    assert usd_exp is not None and usd_exp.status == "resolved"
    # USD assets = (1000 + 300)k = $1.3M × 3.10 = ₪4,030,000; NIS-native excluded.
    assert abs(float(usd_exp.value) - 1_300_000 * 3.10) < 5_000, f"got {usd_exp.value}"


def test_liquid_net_worth_excludes_real_estate(session):
    """portfolio.liquid_net_worth_nis must EXCLUDE asset_type='Real estate'
    positions, while portfolio.net_worth_nis includes them — the honest
    'show both' basis for FI sufficiency (codex/reader 2026-06-17)."""
    from datetime import date as _date
    from decimal import Decimal
    import json as _json
    from argosy.state.models import FxRate, PortfolioSnapshotRow

    session.add(PortfolioSnapshotRow(
        user_id="ariel", imported_at=datetime(2026, 6, 1),
        snapshot_date=_date(2026, 6, 1), fx_usd_nis=3.0,
        totals_json=_json.dumps({"total_usd_value_k": 1069.0}),
        positions_json=_json.dumps([
            {"symbol": "NVDA", "currency": "USD", "usd_value_k": 1000.0},
            {"symbol": "-", "currency": "USD", "usd_value_k": 69.0,
             "asset_type": "Real estate", "details": "Real estate", "location": "Abroad"},
        ]),
    ))
    session.add(FxRate(date=_date.today(), currency="USD", rate=Decimal("3.0"), source="boi"))
    session.flush()
    res = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    nw = res.get("portfolio.net_worth_nis")
    liq = res.get("portfolio.liquid_net_worth_nis")
    assert nw.status == "resolved" and liq.status == "resolved"
    # total includes the $69k property; liquid excludes it (× fx 3.0 = ₪207k)
    assert abs(float(nw.value) - 1_069_000 * 3.0) < 5_000, f"nw {nw.value}"
    assert abs(float(liq.value) - 1_000_000 * 3.0) < 5_000, f"liquid {liq.value}"
    assert float(nw.value) - float(liq.value) > 200_000  # the excluded property


def test_usd_exposure_pending_when_no_snapshot(session):
    from argosy.state.models import PortfolioSnapshotRow
    session.query(PortfolioSnapshotRow).delete()
    session.flush()
    res = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    usd_exp = res.get("portfolio.usd_exposure_nis")
    assert usd_exp is not None and usd_exp.status == "pending"
    assert usd_exp.value is None


def test_us_situs_estate_marks_to_current_boi_fx(session):
    """US-situs estate exposure must mark USD→NIS at the SAME current-BOI-FX
    basis net worth uses — NOT the stale stored snapshot fx. Codex re-derivation
    (run 102) found a ₪75k understatement because estate used the snapshot fx
    (2.94) while net worth used current BOI (2.965): two FX conventions for the
    same book. Seeds a stale snapshot fx AND a current BOI rate that DIFFERS.
    """
    from datetime import date as _date
    from decimal import Decimal
    import json as _json
    from argosy.state.models import FxRate, PortfolioSnapshotRow

    session.add(PortfolioSnapshotRow(
        user_id="ariel", imported_at=datetime(2026, 6, 1),
        snapshot_date=_date(2026, 6, 1), fx_usd_nis=2.94,  # STALE stored fx
        totals_json=_json.dumps({"total_usd_value_k": 1000.0}),
        # NVDA is US-domiciled → counted as US-situs by the IRS-NRA classifier.
        positions_json=_json.dumps([
            {"symbol": "NVDA", "currency": "USD", "usd_value_k": 1000.0},
        ]),
    ))
    # Current BOI rate (dated today so the walkback finds it regardless of
    # when the suite runs) DIFFERS from the snapshot fx.
    session.add(FxRate(date=_date.today(), currency="USD", rate=Decimal("3.10"), source="boi"))
    session.flush()
    res = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    est = res.get("concentration.us_situs_estate_exposure_nis")
    assert est is not None and est.status == "resolved"
    usd = 1000.0 * 1000.0  # $1,000,000 US-situs
    # Must be marked at CURRENT BOI (3.10), NOT the stale snapshot fx (2.94).
    assert abs(float(est.value) - usd * 3.10) < 5_000, (
        f"estate must use current BOI 3.10 (₪{usd*3.10:,.0f}), "
        f"not snapshot 2.94 (₪{usd*2.94:,.0f}); got {est.value}"
    )
    assert abs(float(est.value) - usd * 2.94) > 100_000, "must NOT be snapshot fx"


def test_us_situs_estate_falls_back_to_snapshot_fx_when_boi_cold(session):
    """When NO BOI rate is cached, estate falls back to the snapshot fx and
    does not crash (mirrors net worth's fallback)."""
    import json as _json
    from datetime import date as _date
    from argosy.state.models import PortfolioSnapshotRow

    session.add(PortfolioSnapshotRow(
        user_id="ariel", imported_at=datetime(2026, 6, 1),
        snapshot_date=_date(2026, 6, 1), fx_usd_nis=2.94,
        totals_json=_json.dumps({"total_usd_value_k": 1000.0}),
        positions_json=_json.dumps([
            {"symbol": "NVDA", "currency": "USD", "usd_value_k": 1000.0},
        ]),
    ))
    session.flush()  # NO FxRate seeded → BOI cache cold.
    res = resolve_plan_numbers(session, user_id="ariel", decision_run_id=DRUN)
    est = res.get("concentration.us_situs_estate_exposure_nis")
    assert est is not None and est.status == "resolved"
    # Falls back to snapshot fx 2.94.
    assert abs(float(est.value) - 1000.0 * 1000.0 * 2.94) < 5_000, f"got {est.value}"


def test_net_worth_synth_label_states_liquid_basis():
    """The resolver's portfolio.net_worth_nis is the LIQUID/investable basis
    (USD assets x BOI FX + NIS-native cash, EXCLUDING Israel real-estate
    equity). Its display label must state that basis truthfully so it never
    reads as the same concept as the dashboard's TOTAL net worth (the
    11.95M-vs-14.15M coherence defect)."""
    from argosy.services.plan_numeric_resolver import _SYNTH_DISPLAY

    labels = dict(_SYNTH_DISPLAY)
    nw_label = labels["portfolio.net_worth_nis"]
    assert nw_label != "Net worth", "bare 'Net worth' is ambiguous vs the total basis"
    low = nw_label.lower()
    assert "liquid" in low or "investable" in low
    assert "real-estate" in low or "real estate" in low


def test_net_worth_synth_render_carries_liquid_label():
    """render_numbers_for_synth surfaces the liquid-basis label, not a bare
    'Net worth', for portfolio.net_worth_nis."""
    resolved = ResolvedPlanNumbers(values={
        "portfolio.net_worth_nis": ResolvedValue(
            key="portfolio.net_worth_nis", value=11_950_000.0, unit="nis",
            status="resolved", source_locator="USD assets x ... + NIS-native",
            agent_report_id=None, confidence="HIGH", formula="...",
        ),
    })
    out = render_numbers_for_synth(resolved)
    assert "Liquid net worth" in out
