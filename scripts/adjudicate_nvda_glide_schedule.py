#!/usr/bin/env python3
"""NVDA glide-SCHEDULE adjudication — deterministic facts pack + LLM team.

The question (owner-raised): plan v67's glide deconcentrates NVDA to its
single-stock sleeve target within ~12 months. Should the pace be 12 months,
24 months, or a tax-year-optimized quota schedule? The 12-month glide was
inherited across plan generations, never deliberately adjudicated.

Discipline (binding):
  * Determinism supplies ARITHMETIC FACTS only (position, basis coverage,
    per-schedule Israeli CGT arithmetic via the canonical
    ``deconcentration_optimizer`` calculators, exposure-months, the
    optimizer's FI-age grid). No judgment gates.
  * The LLM TEAM adjudicates: author + BLIND reviewer (same raw facts,
    never the author's verdict), divergence compared IN CODE and forced
    through a reconciliation round (the v66 IWQU-vs-QDVB precedent).
  * The verdict lands as ONE needs-confirm inbox proposal (dedup-refreshed);
    it is NEVER auto-applied — applying it = a governed re-synthesis with
    the verdict as guidance.

Run:  .venv/Scripts/python.exe scripts/adjudicate_nvda_glide_schedule.py
      [--facts-only]  (print the facts pack, skip the LLM team)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

USER_ID = "ariel"
DB_URL = f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}"

# Tax-file citations (the facts pack cites these; rates come from the
# canonical calculator module, which itself sources them from these files).
TAX_SOURCES = (
    "domain_knowledge/tax/israel/capital_gains.md (25% statutory CGT on the "
    "REAL, CPI-indexed gain, post-2003 acquisitions)",
    "domain_knowledge/tax/israel/surtax.md (3% mas-yesef general surtax + 2% "
    "capital-source surtax (2025+) above the annual threshold)",
    "argosy/services/retirement/deconcentration_optimizer.py (codex-reviewed "
    "rate model: 3% applies to the WHOLE gain for a salary already past the "
    "threshold; only the 2% layer is threshold-gated)",
)


# ---------------------------------------------------------------------------
# Deterministic facts pack
# ---------------------------------------------------------------------------
def build_facts(session) -> dict:
    from argosy.services.retirement.deconcentration_optimizer import (
        NVDA_TAXABLE_GAIN_FRACTION,
        TAX_DISCOUNT_REAL,
        _surtax_threshold,
        effective_cgt_rate,
    )

    # --- position from the latest snapshot -------------------------------
    snap = session.execute(sa.text(
        "select id, snapshot_date, positions_json, totals_json, fx_usd_nis "
        "from portfolio_snapshots order by id desc limit 1"
    )).mappings().one()
    positions = json.loads(snap["positions_json"])
    totals = json.loads(snap["totals_json"])
    nvda_usd = sum(
        (p.get("usd_value_k") or 0.0) for p in positions if p.get("symbol") == "NVDA"
    ) * 1000.0
    book_usd = float(totals["total_usd_value_k"]) * 1000.0

    # --- plan v67: glide waypoints + share/eligibility facts --------------
    pv = session.execute(sa.text(
        "select id, version_label, target_allocation_json, horizon_short_json, "
        "horizon_medium_json from plan_versions where role='current' "
        "order by id desc limit 1"
    )).mappings().one()
    ta = json.loads(pv["target_allocation_json"])
    glide = ta.get("glide") or []
    nvda_glide = [
        (g["date"], g["composition_pct_by_class"].get("Strategic single-stock (NVDA)"))
        for g in glide
    ]
    med = json.loads(pv["horizon_medium_json"])

    def _target(hjson, label_sub):
        for t in hjson.get("targets", []):
            if label_sub in (t.get("label") or ""):
                return float(t.get("value"))
        return None

    shares_now = None
    # medium horizon carries the derivation: current less target = sell count
    sell_to_12 = _target(med, "shares to sell to reach the 12%")
    target_12_sh = _target(med, "target shares")
    eligible_now = _target(med, "Section-102 capital-track-eligible inventory")
    if sell_to_12 and target_12_sh:
        shares_now = sell_to_12 + target_12_sh          # 9,270 + 2,201 = 11,471
    price_usd = nvda_usd / shares_now if shares_now else None
    weight_now = nvda_usd / book_usd * 100.0

    # --- cost basis coverage from the RSU vest ledger ---------------------
    basis = session.execute(sa.text(
        "select sum(shares_net) sh, sum(shares_net*fmv_per_share_usd) b "
        "from rsu_vest_events where symbol='NVDA'"
    )).mappings().one()
    rec_sh, rec_basis = float(basis["sh"] or 0), float(basis["b"] or 0)
    vest_gain_fraction = (
        max(0.0, 1.0 - (rec_basis / rec_sh) / price_usd) if rec_sh and price_usd else None
    )

    # --- FX (BOI authority) ------------------------------------------------
    fx = session.execute(sa.text(
        "select date, rate from fx_rates where source='boi' and currency='USD' "
        "order by date desc limit 1"
    )).mappings().first()
    fx_usd_nis = float(fx["rate"]) if fx else float(snap["fx_usd_nis"])
    fx_note = (
        f"BOI official {fx['date']}" if fx
        else "snapshot fx_usd_nis (BOI table unavailable)"
    )

    # --- candidate schedules (per-tax-year Dec-31 quotas) ------------------
    # Destination = the plan's 8% strategic sleeve on TODAY's book.
    target_8_sh = 0.08 * book_usd / price_usd
    sell_total = shares_now - target_8_sh
    start = date(2026, 7, 7)

    def _monthly_path(months: int) -> list[tuple[date, float]]:
        """Linear monthly share path start -> target over `months` months."""
        out = []
        for m in range(months + 1):
            d = date(start.year + (start.month - 1 + m) // 12,
                     (start.month - 1 + m) % 12 + 1, min(start.day, 28))
            sh = shares_now - sell_total * (m / months)
            out.append((d, sh))
        return out

    def _quotas_from_path(path: list[tuple[date, float]]) -> dict[int, float]:
        q: dict[int, float] = {}
        _, prev_sh = path[0]
        for d, sh in path[1:]:
            q[d.year] = q.get(d.year, 0.0) + (prev_sh - sh)
            prev_sh = sh
        return q

    def _exposure(path: list[tuple[date, float]], total_months_horizon: int = 36) -> dict:
        """Months above 30%/20% weight + pp-months above 20% (constant price/book)."""
        m30 = m20 = ppm20 = 0.0
        # extend the path flat at target through the comparison horizon so all
        # schedules are measured over the same 36-month window
        weights = []
        for m in range(total_months_horizon):
            sh = path[min(m, len(path) - 1)][1]
            w = sh * price_usd / book_usd * 100.0
            weights.append(w)
            if w > 30.0:
                m30 += 1
            if w > 20.0:
                m20 += 1
                ppm20 += (w - 20.0)
        return {"months_above_30pct": m30, "months_above_20pct": m20,
                "pp_months_above_20pct": round(ppm20, 0)}

    def _tax(quotas: dict[int, float], gain_fraction: float) -> dict:
        per_year = {}
        total_pv = 0.0
        for y, sh in sorted(quotas.items()):
            gross_usd = sh * price_usd
            gain_nis = gross_usd * fx_usd_nis * gain_fraction
            rate = effective_cgt_rate(gain_nis)
            tax_nis = rate * gain_nis
            pv = tax_nis / ((1.0 + TAX_DISCOUNT_REAL) ** (y - start.year))
            total_pv += pv
            per_year[y] = {
                "shares": round(sh),
                "gross_usd": round(gross_usd),
                "taxable_gain_nis": round(gain_nis),
                "eff_cgt_rate": round(rate, 4),
                "cgt_nis": round(tax_nis),
            }
        return {"per_year": per_year, "total_cgt_pv_nis": round(total_pv)}

    schedules = {}
    # A) 12mo — the CURRENT glide (use the plan's own quarterly waypoints,
    #    interpolated monthly, so the quotas match what v67 actually implies).
    glide_path: list[tuple[date, float]] = []
    for i in range(len(nvda_glide) - 1):
        d0 = date.fromisoformat(nvda_glide[i][0])
        d1 = date.fromisoformat(nvda_glide[i + 1][0])
        w0, w1 = nvda_glide[i][1], nvda_glide[i + 1][1]
        # per-quarter linear in weight; 3 monthly steps
        for k in range(3):
            frac = k / 3.0
            d = d0 + timedelta(days=(d1 - d0).days * frac)
            w = w0 + (w1 - w0) * frac
            glide_path.append((d, w * book_usd / 100.0 / price_usd))
    glide_path.append((date.fromisoformat(nvda_glide[-1][0]),
                       nvda_glide[-1][1] * book_usd / 100.0 / price_usd))
    # normalize the glide start to today's actual share count
    scale = shares_now / glide_path[0][1]
    glide_path = [(d, sh * scale) for d, sh in glide_path]

    for label, path in (
        ("12mo_current_glide", glide_path),
        ("24mo_linear", _monthly_path(24)),
        ("30mo_tax_year_optimized", None),  # built below: equal gains 2026/27/28
    ):
        if label == "30mo_tax_year_optimized":
            # Equal per-tax-year quotas across 2026, 2027, 2028 (Dec-31 quota
            # management; equalizes each year's gain -> minimal 2%-layer +
            # maximal deferral while still finishing by end-2028).
            per = sell_total / 3.0
            quotas = {2026: per, 2027: per, 2028: per}
            # exposure path: within-year even monthly selling, done end-2028
            path = []
            for m in range(31):
                d = date(start.year + (start.month - 1 + m) // 12,
                         (start.month - 1 + m) % 12 + 1, 7)
                path.append((d, max(target_8_sh, shares_now - sell_total * m / 30.0)))
        else:
            quotas = _quotas_from_path(path)
        schedules[label] = {
            "per_tax_year_quotas": {y: round(q) for y, q in sorted(quotas.items())},
            "tax_canonical_gain_fraction": _tax(quotas, NVDA_TAXABLE_GAIN_FRACTION),
            "tax_vest_basis_gain_fraction": (
                _tax(quotas, vest_gain_fraction) if vest_gain_fraction else None
            ),
            "concentration_exposure": _exposure(path),
        }

    # --- FI-age grid: the canonical deconcentration optimizer -------------
    fi_grid = None
    fi_err = None
    try:
        from argosy.services.cashflow_projection import HouseholdState  # noqa: F401
        from argosy.services.retirement.deconcentration_optimizer import (
            optimize_deconcentration_core,
        )
        from argosy.services.retirement.retirement_plan import (
            RetirementAssumptions,
            _reserve_pv,
            _split_spend,
        )
        from argosy.services.retirement.scenario_mc import (
            _calibrated_sigma,
            _gather_inputs,
        )

        a = RetirementAssumptions()
        g = _gather_inputs(session, USER_ID, None)
        sigma_hi = _calibrated_sigma(session, USER_ID)
        full_portfolio = g.household.portfolio_value_nis
        reserve_pv = _reserve_pv(
            g.reserve_nis, a.reserve_discount_real, a.reserve_avg_liability_years
        )
        sell_nis = sell_total * price_usd * fx_usd_nis
        spend_central, _ = _split_spend(session, USER_ID)
        plan = optimize_deconcentration_core(
            household=g.household, pensions=g.pensions,
            full_portfolio_nis=full_portfolio, reserve_pv_nis=reserve_pv,
            total_taxable_gain_nis=sell_nis * NVDA_TAXABLE_GAIN_FRACTION,
            sell_nis=sell_nis, nvda_current_pct=weight_now, nvda_cap_pct=8.0,
            spend_central_nis=spend_central, bl_monthly_nis=g.bl_monthly_nis,
            bl_source=g.bl_source, annuity_tax_rate=g.annuity_tax_rate,
            sigma_current=sigma_hi, horizons=(1, 2, 3),
        )
        fi_grid = {
            "objective": plan.assumptions["objective"],
            "sigma_current_calibrated": plan.sigma_current,
            "per_horizon": [
                {
                    "horizon_years": r.horizon,
                    "total_cgt_pv_nis": round(r.total_cgt_nis),
                    "eff_cgt_rate": round(r.eff_cgt_rate, 4),
                    "earliest_safe_drawdown_age": r.drawdown_age,
                    "sigma_path": r.sigma_path_desc,
                }
                for r in plan.per_horizon
            ],
        }
    except Exception as e:  # noqa: BLE001 — facts pack degrades honestly
        fi_err = f"{type(e).__name__}: {e}"

    return {
        "as_of": str(snap["snapshot_date"]),
        "position": {
            "nvda_shares": round(shares_now),
            "nvda_price_usd_implied": round(price_usd, 2),
            "nvda_value_usd": round(nvda_usd),
            "tradeable_book_usd": round(book_usd),
            "nvda_weight_pct": round(weight_now, 2),
            "source": f"portfolio_snapshots id={snap['id']} ({snap['snapshot_date']}); "
                      f"share count = plan v{pv['id']} medium-horizon targets "
                      f"(target {target_12_sh:.0f} + sell {sell_to_12:.0f})",
        },
        "cost_basis": {
            "recorded_vest_net_shares": round(rec_sh),
            "recorded_vest_basis_usd": round(rec_basis),
            "avg_basis_usd_per_share": round(rec_basis / rec_sh, 2) if rec_sh else None,
            "vest_basis_implied_gain_fraction": (
                round(vest_gain_fraction, 3) if vest_gain_fraction else None
            ),
            "canonical_model_gain_fraction": NVDA_TAXABLE_GAIN_FRACTION,
            "note": (
                "rsu_vest_events covers "
                f"{rec_sh:.0f} net shares vs {shares_now:.0f} held; per-lot "
                "Section-102 ordinary-vs-capital splitting is NOT modeled here — "
                "the canonical codex-reviewed model taxes 0.8 of the sale as "
                "capital gain. Both fractions are shown; the schedule DELTA is "
                "driven by the surtax layers either way."
            ),
            "source": "rsu_vest_events (all NVDA grants; grant 182406 et al.)",
        },
        "current_glide_nvda_waypoints_pct": nvda_glide,
        "section_102": {
            "capital_track_eligible_now_shares": eligible_now,
            "shares_to_8pct_target": round(sell_total),
            "note": (
                "selling beyond the eligible pool at the capital rate is not "
                "possible — ineligible shares are ordinary income (~50-62%). "
                f"~{max(0, round(sell_total - (eligible_now or 0)))} of the "
                "8%-target sale exceeds today's eligible pool; per-lot "
                "eligibility maturation dates are NOT computable from the "
                "current book (honest gap) — later tranches mature into "
                "eligibility, which structurally favors back-loading the tail."
            ),
            "source": f"plan v{pv['id']} horizon targets",
        },
        "tax_parameters": {
            "cgt_base_rate": 0.25,
            "surtax_general_rate_whole_gain": 0.03,
            "surtax_capital_source_rate_above_threshold": 0.02,
            "surtax_threshold_nis": _surtax_threshold(),
            "salary_note": (
                "NVIDIA salary already exceeds the threshold, so the 3% layer "
                "applies to the WHOLE gain in every schedule; ONLY the 2% "
                "capital-source layer (on the slice above the threshold) and "
                "the deferral discount are schedule-sensitive."
            ),
            "tax_pv_discount_real": TAX_DISCOUNT_REAL,
            "real_gain_note": (
                "statutory base is the REAL (CPI-indexed) gain; nominal gain "
                "used here (conservative — never understates the tax)."
            ),
            "fx_usd_nis": fx_usd_nis,
            "fx_source": fx_note,
            "sources": list(TAX_SOURCES),
        },
        "schedules": schedules,
        "fi_age_grid": fi_grid,
        "fi_age_grid_error": fi_err,
        "assumption_notes": [
            "constant NVDA price and constant book across the schedule window "
            "(no price path is forecast — this is arithmetic, not prediction)",
            "sale proceeds stay invested per the plan's target sleeves, so the "
            "book total is schedule-invariant in this arithmetic",
            "exposure measured over a common 36-month window",
        ],
    }


def facts_to_md(f: dict) -> str:
    return "```json\n" + json.dumps(f, indent=1, default=str) + "\n```"


# ---------------------------------------------------------------------------
# Fleet run
# ---------------------------------------------------------------------------
def run_team(facts_md: str) -> dict:
    from argosy.agents.plan_change_team import (
        GlideScheduleAdjudicatorAgent,
        glide_schedule_divergences,
    )
    from argosy.services.fleet_reliability import (
        FleetRetryConfig,
        call_reliably_sync,
    )

    cfg = FleetRetryConfig(hard_timeout_s=600.0)
    totals = {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    t0 = time.monotonic()

    def _run(scope: str, **kw):
        def _attempt():
            agent = GlideScheduleAdjudicatorAgent(user_id=USER_ID)
            return agent.run_sync(facts_md=facts_md, **kw)

        rep = call_reliably_sync(_attempt, scope=scope, config=cfg)
        totals["cost_usd"] += float(getattr(rep, "cost_usd", 0) or 0)
        totals["tokens_in"] += int(getattr(rep, "tokens_in", 0) or 0)
        totals["tokens_out"] += int(getattr(rep, "tokens_out", 0) or 0)
        return rep.output

    print("[team] author adjudicating ...", flush=True)
    author = _run("glide_schedule_author")
    print(f"[team] author: {author.chosen_schedule} ({author.horizon_months}mo)", flush=True)

    print("[team] blind reviewer re-deriving ...", flush=True)
    reviewer = _run("glide_schedule_blind_reviewer", blind_rederive=True)
    print(f"[team] reviewer: {reviewer.chosen_schedule} ({reviewer.horizon_months}mo)", flush=True)

    divergences = glide_schedule_divergences(author, reviewer)
    final = author
    reconciled = False
    if divergences:
        print(f"[team] DIVERGENCE ({len(divergences)}) — reconciliation round", flush=True)
        reconcile_md = (
            "Divergences (computed in code):\n"
            + "\n".join(f"- {d}" for d in divergences)
            + "\n\nReviewer verdict JSON:\n"
            + reviewer.model_dump_json(indent=1)
        )
        final = _run("glide_schedule_reconcile", reconcile_md=reconcile_md)
        reconciled = True
        print(f"[team] reconciled final: {final.chosen_schedule} "
              f"({final.horizon_months}mo)", flush=True)

    residual = glide_schedule_divergences(final, reviewer) if reconciled else []
    return {
        "author": author,
        "reviewer": reviewer,
        "final": final,
        "divergences": divergences,
        "residual_divergences": residual,
        "reconciled": reconciled,
        "duration_s": round(time.monotonic() - t0, 1),
        **totals,
    }


# ---------------------------------------------------------------------------
# Inbox sink (needs-confirm; dedup-refreshed; never auto-applied)
# ---------------------------------------------------------------------------
def sink_proposal(session, facts: dict, team: dict) -> int:
    from argosy.state.models import ActionProposal

    final = team["final"]
    author, reviewer = team["author"], team["reviewer"]
    keep = not final.changes_current_glide
    quotas = {
        2026: final.quota_2026_shares,
        2027: final.quota_2027_shares,
        2028: final.quota_2028_shares,
    }
    sched_facts = facts["schedules"]

    prov = (
        f"Author proposed **{author.chosen_schedule}** ({author.horizon_months}mo); "
        f"blind reviewer independently re-derived **{reviewer.chosen_schedule}** "
        f"({reviewer.horizon_months}mo)."
    )
    if team["divergences"]:
        prov += (
            " Divergence was code-forced through a reconciliation round: "
            + "; ".join(team["divergences"])
            + f". Final (reconciled): **{final.chosen_schedule}**."
        )
        if team["residual_divergences"]:
            prov += (
                " RESIDUAL divergence remains: "
                + "; ".join(team["residual_divergences"])
            )
    else:
        prov += " The two blind derivations AGREED."

    quota_lines = "\n".join(
        f"  - Dec-31 {y}: sell {q:,.0f} NVDA shares"
        for y, q in quotas.items() if q > 0
    )
    tax_table = "\n".join(
        f"  - {label}: total CGT (PV) ₪{s['tax_canonical_gain_fraction']['total_cgt_pv_nis']:,}"
        f" | months >30% weight: {s['concentration_exposure']['months_above_30pct']:.0f}"
        f" | months >20%: {s['concentration_exposure']['months_above_20pct']:.0f}"
        for label, s in sched_facts.items()
    )

    rationale = (
        f"## NVDA glide schedule — fleet verdict (needs your confirmation)\n\n"
        f"**Verdict: {final.chosen_schedule} ({final.horizon_months} months)"
        f"{' — KEEP the current glide' if keep else ' — CHANGE from the current 12-month glide'}**\n\n"
        f"**Trade-off:** {final.tradeoff_sentence}\n\n"
        f"**Per-tax-year quotas (Israeli calendar tax years):**\n{quota_lines}\n\n"
        f"**The deterministic comparison (all three candidates):**\n{tax_table}\n\n"
        f"**Fleet rationale:** {final.rationale}\n\n"
        f"**Provenance:** {prov}\n\n"
        "**Applying this verdict:** nothing has been changed. "
        + (
            "The current plan glide already matches this schedule — confirming "
            "records the pace as a deliberate decision."
            if keep else
            "The glide's horizon rows are refinement-unreachable, so applying "
            "it = a governed re-synthesis with this verdict as guidance; it "
            "composes with the re-synthesis already queued for the 12%-ghost "
            "RED."
        )
    )

    payload = {
        "verdict": final.model_dump(),
        "author": author.model_dump(),
        "blind_reviewer": reviewer.model_dump(),
        "divergences": team["divergences"],
        "residual_divergences": team["residual_divergences"],
        "facts_pack": facts,
        "apply_path": "governed re-synthesis with verdict as guidance (never a direct plan mutation)",
    }

    # Severity must be honest: a verdict that CHANGES the current glide is a
    # material plan change awaiting the user (warning); a keep-as-is
    # confirmation is informational. (Surfacing does NOT depend on this —
    # any open decision-kind proposal reaches the inbox regardless of
    # severity — severity only drives urgency ordering.)
    severity = "warning" if final.changes_current_glide else "info"
    dedup = f"plan_glide_schedule_verdict:{USER_ID}:nvda"
    existing = session.execute(
        sa.select(ActionProposal).filter_by(
            user_id=USER_ID, dedup_key=dedup, status="open"
        )
    ).scalar_one_or_none()
    summary = (
        f"NVDA deconcentration pace adjudicated: {final.chosen_schedule} "
        f"({final.horizon_months}mo) — confirm the schedule"
    )
    now = datetime.now(UTC)
    if existing:
        existing.summary = summary
        existing.rationale_md = rationale
        existing.suggested_payload = json.dumps(payload, default=str)
        existing.severity = severity
        existing.surfaced_at = now
        existing.expires_at = now + timedelta(days=30)
        session.commit()
        return existing.id
    row = ActionProposal(
        user_id=USER_ID,
        summary=summary,
        rationale_md=rationale,
        suggested_payload=json.dumps(payload, default=str),
        severity=severity,
        surfaced_at=now,
        expires_at=now + timedelta(days=30),
        status="open",
        kind="update_plan_assumption",
        dedup_key=dedup,
        execution_state="proposed",
    )
    session.add(row)
    session.commit()
    return row.id


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-only", action="store_true")
    args = ap.parse_args()

    engine = sa.create_engine(DB_URL)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        facts = build_facts(session)
        facts_md = facts_to_md(facts)
        print(facts_md)
        if args.facts_only:
            return
        team = run_team(facts_md)
        pid = sink_proposal(session, facts, team)
        print(json.dumps({
            "proposal_id": pid,
            "final": team["final"].model_dump(),
            "author": team["author"].model_dump(),
            "reviewer": team["reviewer"].model_dump(),
            "divergences": team["divergences"],
            "residual_divergences": team["residual_divergences"],
            "duration_s": team["duration_s"],
            "cost_usd": round(team["cost_usd"], 2),
            "tokens_in": team["tokens_in"],
            "tokens_out": team["tokens_out"],
        }, indent=1, default=str))
    finally:
        session.close()


if __name__ == "__main__":
    main()
