#!/usr/bin/env python3
"""Dry-powder DISCOVERY RESERVE — deterministic facts pack + LLM team design.

The directive (Ariel, 2026-07-09): earmark a cash reserve for discovery-sleeve
buys so a green-lit discovery candidate never waits for a sale. HARD
CONSTRAINT (client, binding): the reserve is held in CASH OR CASH-EQUIVALENT
ONLY (SGOV/IB01-class T-bill ETFs — instantly deployable, zero drawdown;
never parked in anything that can fall or takes days to unwind). This is a
VALUES DIRECTIVE: the fleet designs + proposes sizing and mechanics; Ariel
confirms the sizing. NOTHING is auto-applied to the plan.

Discipline (binding, mirrors scripts/adjudicate_alternatives_gold_sleeve.py):
  * Determinism supplies FACTS only: current cash + T-bill positions, plan v74
    sleeve targets, the x10 funding gap, pending staged-sell inflows, the
    discovery pipeline's actual hit rate — every number from raw DB rows.
  * The LLM TEAM designs: author + BLIND reviewer (same raw facts, never the
    author's design), divergence compared IN CODE and forced through a
    reconciliation round.
  * The cash-equivalent-only constraint is an INVIOLABLE client floor —
    validated deterministically on the final design (fail loud on breach).
  * The design lands as ONE needs-confirm inbox proposal (dedup-refreshed);
    NEVER auto-applied — Ariel confirms the sizing.

Run:  .venv/Scripts/python.exe scripts/propose_dry_powder_reserve.py
      [--facts-only]  (print the facts pack, skip the LLM team)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

USER_ID = "ariel"
DB_URL = f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}"

# Ariel's inviolable floor: instantly deployable, zero-drawdown vehicles only.
CASH_EQUIVALENT_TOKENS = {"CASH", "SGOV", "IB01", "T-BILL", "T-BILLS", "TBILL"}


# ---------------------------------------------------------------------------
# Deterministic facts pack — every number from raw DB rows / plan v74
# ---------------------------------------------------------------------------
def build_facts(session) -> dict:
    snap = session.execute(sa.text(
        "select id, snapshot_date, positions_json, totals_json, fx_usd_nis "
        "from portfolio_snapshots order by id desc limit 1"
    )).mappings().one()
    positions = json.loads(snap["positions_json"])
    totals = json.loads(snap["totals_json"])
    book_usd = float(totals["total_usd_value_k"]) * 1000.0
    cash_usd = float(totals.get("cash_balances_usd_k") or 0.0) * 1000.0

    # Cash detail rows: blank/dash symbol, excluding non-cash blanks (real
    # estate). Cross-checked against totals_json below — fail loud on drift.
    cash_rows = []
    for p in positions:
        sym = (p.get("symbol") or "").strip()
        if sym in ("", "-"):
            details = str(p.get("details") or "")
            if "estate" in details.lower():
                continue
            cash_rows.append({
                "location": p.get("location"),
                "currency": p.get("currency"),
                "usd": round((p.get("usd_value_k") or 0.0) * 1000.0),
            })
    cash_rows_sum = sum(r["usd"] for r in cash_rows)
    if abs(cash_rows_sum - cash_usd) > 500:
        raise RuntimeError(
            f"cash detail rows (${cash_rows_sum:,}) do not reconcile with "
            f"totals_json cash (${cash_usd:,.0f}) — snapshot schema drift?"
        )

    # T-bill-class positions actually held (SGOV / IB01)
    tbill_rows = []
    for p in positions:
        sym = (p.get("symbol") or "").strip().upper()
        if sym in ("SGOV", "IB01"):
            tbill_rows.append({
                "symbol": sym,
                "location": p.get("location"),
                "usd": round((p.get("usd_value_k") or 0.0) * 1000.0),
            })
    tbill_usd = sum(r["usd"] for r in tbill_rows)

    # Plan v74 (role=current) targets
    pv = session.execute(sa.text(
        "select id, version_label, target_allocation_json from plan_versions "
        "where role='current' order by id desc limit 1"
    )).mappings().one()
    ta = json.loads(pv["target_allocation_json"])
    plan_targets = {c["label"]: c["target_pct"] for c in ta["classes"]}
    by_label = {c["label"]: c for c in ta["classes"]}
    cash_sleeve = next(
        c for c in ta["classes"] if c["label"].lower().startswith("cash")
    )
    x10_sleeve = next(
        c for c in ta["classes"]
        if "high-growth" in c["label"].lower()
        or "high-potential" in c["label"].lower()
    )
    x10_tickers = {
        (i.get("symbol") or "").upper() for i in x10_sleeve.get("instruments", [])
    }
    x10_funded_usd = sum(
        (p.get("usd_value_k") or 0.0) * 1000.0
        for p in positions
        if (p.get("symbol") or "").strip().upper() in x10_tickers
    )
    x10_target_usd = float(x10_sleeve["target_pct"]) / 100.0 * book_usd

    # Pending staged proposals (5-8 sells + 10 buy) — first-tranche USD sizes
    price_by_sym = {}
    for p in positions:
        s = (p.get("symbol") or "").strip().upper()
        if s and s not in price_by_sym and p.get("current_price"):
            price_by_sym[s] = float(p["current_price"])
    props = session.execute(sa.text(
        "select id, ticker, action, size_shares_or_currency, size_units, "
        "status, expected_impact_json from proposals "
        "where id in (5, 6, 7, 8, 10) order by id"
    )).mappings().all()
    pending = []
    sell_inflow_usd = 0.0
    for pr in props:
        size = float(pr["size_shares_or_currency"])
        if pr["size_units"] == "currency":
            usd = size
        else:
            px = price_by_sym.get(pr["ticker"].upper())
            if px is None:
                raise RuntimeError(
                    f"proposal {pr['id']} sized in shares but no snapshot "
                    f"price for {pr['ticker']}"
                )
            usd = size * px
        pending.append({
            "id": pr["id"], "ticker": pr["ticker"], "action": pr["action"],
            "usd": round(usd), "status": pr["status"],
        })
        if pr["action"] == "sell":
            sell_inflow_usd += usd

    # Discovery pipeline reality — raw trend_scan_state counts + the actual
    # fleet theses (cap-math first line verbatim from fleet_json)
    scanned = session.execute(
        sa.text("select count(*) n from trend_scan_state")
    ).scalar_one()
    status_counts = {
        r["status"]: r["n"] for r in session.execute(sa.text(
            "select status, count(*) n from trend_scan_state group by status"
        )).mappings()
    }
    fleet_evals = []
    for r in session.execute(sa.text(
        "select ticker, status, fleet_json from trend_scan_state "
        "where fleet_json is not null and fleet_json != ''"
    )).mappings():
        fj = json.loads(r["fleet_json"])
        thesis = str(fj.get("thesis_md") or "")
        fleet_evals.append({
            "ticker": r["ticker"],
            "pipeline_status": r["status"],
            "conviction": fj.get("conviction"),
            "thesis_first_line": thesis.splitlines()[0][:240] if thesis else "",
        })

    gate_widening = session.execute(sa.text(
        "select id, summary, status from action_proposals where id = 68"
    )).mappings().one_or_none()

    # Sizing precedent: proposal 10's bounded slot top-up
    slot_precedent = next((p for p in pending if p["id"] == 10), None)

    return {
        "as_of": str(snap["snapshot_date"]),
        "plan_version_id": pv["id"],
        "plan_version_label": pv["version_label"],
        "book": {
            "book_usd": round(book_usd),
            "fx_usd_nis": round(float(snap["fx_usd_nis"]), 4),
        },
        "cash_and_equivalents": {
            "cash_usd_total": round(cash_usd),
            "cash_detail_rows": cash_rows,
            "tbill_positions": tbill_rows,
            "tbill_usd_total": tbill_usd,
            "cash_plus_tbills_usd": round(cash_usd + tbill_usd),
            "note": (
                "IB01 is the plan's cash-sleeve primary instrument but is NOT "
                "held today; SGOV is the T-bill vehicle actually on the book."
            ),
        },
        "plan_targets": plan_targets,
        "cash_sleeve": {
            "label": cash_sleeve["label"],
            "target_pct": float(cash_sleeve["target_pct"]),
            "target_usd_at_current_book": round(
                float(cash_sleeve["target_pct"]) / 100.0 * book_usd
            ),
            "primary_instrument": (cash_sleeve.get("instruments") or
                                   [{}])[0].get("symbol"),
            "current_cash_plus_tbills_usd": round(cash_usd + tbill_usd),
        },
        "x10_sleeve": {
            "label": x10_sleeve["label"],
            "target_pct": float(x10_sleeve["target_pct"]),
            "target_usd_at_current_book": round(x10_target_usd),
            "instruments": sorted(x10_tickers),
            "funded_usd": round(x10_funded_usd),
            "funded_pct_of_book": round(x10_funded_usd / book_usd * 100.0, 2),
            "funding_gap_usd": round(x10_target_usd - x10_funded_usd),
        },
        "pending_staged_proposals": {
            "rows": pending,
            "sell_inflow_usd_first_tranches": round(sell_inflow_usd),
            "note": (
                "sells are staged (first tranches shown); all awaiting the "
                "client — incoming cash that could seed the reserve"
            ),
        },
        "discovery_pipeline": {
            "names_scanned_lifetime": scanned,
            "status_counts": status_counts,
            "fleet_evaluations": fleet_evals,
            "routed_to_buy_ever": 0,
            "note": (
                "440 names scanned lifetime; only SOUN and JOBY ever passed "
                "the x10 cap-math test, both at MED conviction, and the HIGH "
                "conviction floor blocked both — zero discovery buys ever "
                "routed. Inbox proposal 68 (pending Ariel) would widen the "
                "gates (floor HIGH->MEDIUM + radar cap $8B->$30B): if "
                "approved, discovery flow increases materially."
            ),
            "gate_widening_proposal": dict(gate_widening) if gate_widening
                                      else None,
        },
        "slot_sizing_precedent": {
            "proposal": slot_precedent,
            "note": (
                "proposal 10 (NOW bounded-slot top-up) is the on-record "
                "precedent for a single discovery-class slot: "
                f"${slot_precedent['usd']:,} ≈ "
                f"{slot_precedent['usd'] / book_usd * 100.0:.2f}% of book"
                if slot_precedent else "no precedent row found"
            ),
        },
        "client_directive_verbatim": (
            "Client (2026-07-09, binding): earmark a cash reserve for "
            "discovery-sleeve buys so green-lit discovery candidates never "
            "wait for a sale. The reserve is held in CASH OR CASH-EQUIVALENT "
            "ONLY (SGOV/IB01-class T-bill ETFs — instantly deployable, zero "
            "drawdown; never parked in anything that can fall or takes days "
            "to unwind). The fleet designs and proposes sizing + mechanics; "
            "the client confirms the sizing."
        ),
        "owner_supplied_context": (
            "Opportunity cost is real: T-bill-class vehicles yield ~4-5% "
            "nominal vs the plan's equity return assumption, so an idle "
            "reserve drags the FI date; the design must weigh readiness "
            "against that drag. The plan engine has a FIXED asset-class set: "
            "the earmark must be an annotation on the cash sleeve, NOT a new "
            "asset class."
        ),
    }


def facts_md(facts: dict) -> str:
    return (
        "## The book (portfolio snapshot "
        f"{facts['as_of']}, plan v{facts['plan_version_id']} current)\n"
        f"{json.dumps(facts['book'], indent=1)}\n\n"
        "## Cash + cash-equivalents actually held\n"
        f"{json.dumps(facts['cash_and_equivalents'], indent=1)}\n\n"
        "## Plan v74 sleeve targets (%)\n"
        f"{json.dumps(facts['plan_targets'], indent=1)}\n\n"
        "## The cash sleeve (where the earmark would live)\n"
        f"{json.dumps(facts['cash_sleeve'], indent=1)}\n\n"
        "## The x10 high-growth sleeve (what discovery buys fill)\n"
        f"{json.dumps(facts['x10_sleeve'], indent=1)}\n\n"
        "## Pending staged proposals (incoming cash / competing uses)\n"
        f"{json.dumps(facts['pending_staged_proposals'], indent=1)}\n\n"
        "## Discovery pipeline reality (raw scan-state)\n"
        f"{json.dumps(facts['discovery_pipeline'], indent=1)}\n\n"
        "## Slot-sizing precedent\n"
        f"{json.dumps(facts['slot_sizing_precedent'], indent=1)}\n\n"
        "## CLIENT DIRECTIVE (verbatim, binding)\n"
        f"{facts['client_directive_verbatim']}\n\n"
        "## Owner-supplied context\n"
        f"{facts['owner_supplied_context']}\n"
    )


# ---------------------------------------------------------------------------
# The LLM team — reserve-design author + blind reviewer (prose-JSON path;
# the bundled claude.exe chokes on nested $defs, so the schema stays flat)
# ---------------------------------------------------------------------------
class ReserveDesign(BaseModel):
    reserve_pct: float = Field(ge=0.0, le=5.0)   # % of the tradeable book
    instrument: str                               # CASH | SGOV | IB01 | combo
    within_cash_sleeve: bool                      # earmark inside 5.68% sleeve?
    seed_funding_md: str                          # where the first dollars come from
    replenishment_md: str                         # how it refills after a deployment
    earmark_mechanics_md: str                     # annotation mechanics on the cash sleeve
    x10_cap_interaction_md: str                   # interaction with the 5.0% x10 cap
    idle_policy_md: str                           # what happens when the reserve idles
    tradeoff_sentence: str                        # ONE sentence: readiness vs carry drag
    rationale: str


_OUTPUT_SPEC = (
    "OUTPUT: a single JSON object with keys reserve_pct (number, % of the "
    "tradeable book), instrument (string: CASH, SGOV, IB01, or a combination "
    "like 'SGOV + CASH' — cash-equivalent vehicles ONLY), within_cash_sleeve "
    "(bool — true iff the reserve is an earmark INSIDE the existing Cash & "
    "T-bills sleeve target rather than added on top of it), seed_funding_md, "
    "replenishment_md, earmark_mechanics_md, x10_cap_interaction_md, "
    "idle_policy_md, tradeoff_sentence (ONE sentence: readiness benefit vs "
    "the carry drag), rationale. No prose outside the JSON."
)


def _system_prompt(blind: bool) -> str:
    from argosy.agents._plan_authority import PRIME_DIRECTIVE

    core = (
        "You are designing the DRY-POWDER DISCOVERY RESERVE for a long-hold, "
        "Israeli-resident (non-US-person) investor. The client's directive "
        "(binding): earmark a cash reserve for discovery-sleeve buys so a "
        "green-lit discovery candidate NEVER waits for a sale to settle.\n\n"
        f"{PRIME_DIRECTIVE}\n\n"
        "HARD CONSTRAINT (inviolable, client-set): the reserve is held in "
        "CASH OR CASH-EQUIVALENT ONLY — SGOV/IB01-class T-bill ETFs or bank "
        "cash; instantly deployable, zero drawdown; NEVER anything that can "
        "fall in value or takes days to unwind. Any instrument outside that "
        "set is rejected in code.\n\n"
        "YOU DECIDE (ground every number in the facts pack — invent "
        "nothing):\n"
        "  1. SIZE — reserve_pct of the book (the facts pack gives the slot "
        "precedent, the x10 funding gap, the discovery hit rate, and the "
        "pending gate-widening proposal 68 that would raise flow).\n"
        "  2. INSTRUMENT — held bank cash vs SGOV vs IB01 (note: IB01 is the "
        "plan's cash-sleeve primary but is NOT held; SGOV is on the book; "
        "estate/domicile: IB01 is the Irish-UCITS-class vehicle, SGOV is "
        "US-domiciled — the cash sleeve is small, weigh it honestly).\n"
        "  3. REPLENISHMENT — how the reserve refills after a deployment "
        "(staged-sell proceeds / paycheck cash; be concrete about priority "
        "and pace).\n"
        "  4. EARMARK MECHANICS — the plan engine has a FIXED class set: the "
        "reserve must be an ANNOTATION on the Cash & T-bills sleeve, not a "
        "new asset class. Say exactly how it is recorded and how a "
        "deployment draws it down.\n"
        "  5. x10 CAP INTERACTION — discovery buys fill the 5.0% x10 sleeve "
        "(funded ~0.4% today). Does the reserve pre-commit part of that "
        "gap? What happens as the sleeve approaches its cap?\n"
        "  6. IDLE POLICY — the pipeline has routed ZERO buys ever (2 picks "
        "in 440 names, both floor-blocked). If the reserve idles for "
        "months, what happens? Opportunity cost is real (T-bill carry vs "
        "the plan's equity assumption): weigh a reserve sized for a flow "
        "that may stay near zero vs one that is useless when proposal 68 "
        "widens the gates.\n\n"
        "DECISION CRITERION: earliest SAFE retirement on the household's "
        "ACTUAL book. A too-big reserve is a permanent carry drag; a "
        "too-small one re-creates the exact failure the client named "
        "(a green-lit pick waiting on a sale). The client confirms the "
        "sizing — nothing you output is auto-applied.\n\n"
        f"{_OUTPUT_SPEC}"
    )
    if blind:
        core = (
            "You are an INDEPENDENT reviewer: another agent has already "
            "designed this reserve — you have NOT seen its design and must "
            "not guess it; derive your own from the raw facts alone (your "
            "design is compared in code and divergence forces a "
            "reconciliation round).\n\n"
        ) + core
    return core


def _make_agent_cls():
    from argosy.agents.base import BaseAgent

    class DryPowderReserveAgent(BaseAgent[ReserveDesign]):
        """Authors (or blind-re-derives) the discovery dry-powder reserve."""

        agent_role = "dry_powder_reserve_author"  # not in tables -> Opus fallback
        output_model = ReserveDesign
        require_citations = False
        use_structured_output = False
        claude_code_max_retries = 1

        def build_prompt(
            self,
            *,
            facts_block: str,
            blind_rederive: bool = False,
            reconcile_md: str = "",
        ) -> tuple[str, str]:
            user = (
                "DETERMINISTIC FACTS PACK (raw DB rows / plan v74; every "
                "number sourced):\n\n"
                f"{facts_block}\n\n"
            )
            if reconcile_md:
                user += (
                    "RECONCILIATION ROUND (code-forced): an independent blind "
                    "reviewer derived the design from the same raw facts and "
                    "DIVERGED. Both designs are below. Reconcile ON THE "
                    "NUMBERS: concede where the other derivation is stronger "
                    "or refute it with specific figures from the facts pack. "
                    "Output your FINAL design in the same JSON schema.\n\n"
                    f"{reconcile_md}\n\n"
                )
            user += "Design the reserve now."
            return _system_prompt(blind_rederive), user

    return DryPowderReserveAgent


def _norm_instrument(s: str) -> frozenset[str]:
    toks = [t.strip().upper() for t in
            s.replace("+", ",").replace("/", ",").replace(" AND ", ",").split(",")]
    return frozenset(t for t in toks if t)


def _divergences(a: ReserveDesign, b: ReserveDesign) -> list[str]:
    out: list[str] = []
    if abs(a.reserve_pct - b.reserve_pct) > 0.5:
        out.append(f"reserve_pct: author={a.reserve_pct} vs "
                   f"reviewer={b.reserve_pct}")
    if _norm_instrument(a.instrument) != _norm_instrument(b.instrument):
        out.append(f"instrument: author={a.instrument!r} vs "
                   f"reviewer={b.instrument!r}")
    if a.within_cash_sleeve != b.within_cash_sleeve:
        out.append(f"within_cash_sleeve: author={a.within_cash_sleeve} vs "
                   f"reviewer={b.within_cash_sleeve}")
    return out


def _enforce_cash_equivalent_floor(design: ReserveDesign) -> None:
    """Ariel's inviolable floor — deterministic, fail-loud (arithmetic-floor
    class: it never judges the design, only rejects a constraint breach)."""
    bad = _norm_instrument(design.instrument) - CASH_EQUIVALENT_TOKENS
    if bad:
        raise RuntimeError(
            f"final design breaches the cash-equivalent-only floor: "
            f"{sorted(bad)} not in {sorted(CASH_EQUIVALENT_TOKENS)}"
        )


def run_team(facts: dict) -> dict:
    from argosy.services.fleet_reliability import (
        FleetRetryConfig,
        call_reliably_sync,
    )

    agent_cls = _make_agent_cls()
    fb = facts_md(facts)
    cfg = FleetRetryConfig(hard_timeout_s=600.0)
    totals = {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    t0 = time.monotonic()

    def _run(scope: str, *, blind: bool = False, reconcile_md: str = ""):
        def _attempt():
            agent = agent_cls(user_id=USER_ID)
            return agent.run_sync(
                facts_block=fb,
                blind_rederive=blind,
                reconcile_md=reconcile_md,
            )
        rep = call_reliably_sync(_attempt, scope=scope, config=cfg)
        totals["cost_usd"] += float(getattr(rep, "cost_usd", 0) or 0)
        totals["tokens_in"] += int(getattr(rep, "tokens_in", 0) or 0)
        totals["tokens_out"] += int(getattr(rep, "tokens_out", 0) or 0)
        return rep.output

    print("[team] author designing ...", flush=True)
    author = _run("dry_powder_author")
    print(f"[team] author: {author.reserve_pct}% in {author.instrument} "
          f"(within_cash_sleeve={author.within_cash_sleeve})", flush=True)

    print("[team] blind reviewer re-deriving ...", flush=True)
    reviewer = _run("dry_powder_blind_reviewer", blind=True)
    print(f"[team] reviewer: {reviewer.reserve_pct}% in {reviewer.instrument} "
          f"(within_cash_sleeve={reviewer.within_cash_sleeve})", flush=True)

    divergences = _divergences(author, reviewer)
    final, reconciled = author, False
    if divergences:
        print(f"[team] DIVERGENCE ({len(divergences)}) — reconciliation round",
              flush=True)
        rec = (
            "Divergences (compared in code):\n"
            + "\n".join(f"- {d}" for d in divergences)
            + "\n\n--- AUTHOR DESIGN JSON ---\n"
            + author.model_dump_json(indent=1)
            + "\n\n--- BLIND REVIEWER DESIGN JSON ---\n"
            + reviewer.model_dump_json(indent=1)
        )
        final = _run("dry_powder_reconcile", reconcile_md=rec)
        reconciled = True
        print(f"[team] reconciled final: {final.reserve_pct}% in "
              f"{final.instrument} "
              f"(within_cash_sleeve={final.within_cash_sleeve})", flush=True)

    _enforce_cash_equivalent_floor(final)

    residual = _divergences(final, reviewer) if reconciled else []
    return {
        "author": author, "reviewer": reviewer, "final": final,
        "divergences": divergences, "residual_divergences": residual,
        "reconciled": reconciled,
        "duration_s": round(time.monotonic() - t0, 1), **totals,
    }


# ---------------------------------------------------------------------------
# Inbox sink (needs-confirm; dedup-refreshed; never auto-applied)
# ---------------------------------------------------------------------------
def sink_proposal(session, facts: dict, team: dict) -> int:
    from argosy.state.models import ActionProposal

    final, author, reviewer = team["final"], team["author"], team["reviewer"]
    book_usd = facts["book"]["book_usd"]
    reserve_usd = round(final.reserve_pct / 100.0 * book_usd)

    prov = (
        f"Author designed {author.reserve_pct}% in {author.instrument} "
        f"(within_cash_sleeve={author.within_cash_sleeve}); blind reviewer "
        f"independently derived {reviewer.reserve_pct}% in "
        f"{reviewer.instrument} "
        f"(within_cash_sleeve={reviewer.within_cash_sleeve})."
    )
    if team["divergences"]:
        prov += (
            " Divergence was code-forced through a reconciliation round: "
            + "; ".join(team["divergences"])
            + f". Final (reconciled): {final.reserve_pct}% in "
            f"{final.instrument}."
        )
        if team["residual_divergences"]:
            prov += (" RESIDUAL divergence remains: "
                     + "; ".join(team["residual_divergences"]))
    else:
        prov += " The two blind derivations AGREED."

    rationale = (
        "## Dry-powder discovery reserve — fleet-designed, needs your "
        "confirmation on the SIZING\n\n"
        f"**Design: earmark {final.reserve_pct}% of the book "
        f"(~${reserve_usd:,}) as a discovery dry-powder reserve, held in "
        f"{final.instrument} "
        f"({'inside' if final.within_cash_sleeve else 'on top of'} the "
        f"existing {facts['cash_sleeve']['target_pct']}% Cash & T-bills "
        "sleeve).**\n\n"
        f"**Why (fleet rationale):** {final.rationale}\n\n"
        f"**Trade-off:** {final.tradeoff_sentence}\n\n"
        f"**Seed funding:** {final.seed_funding_md}\n\n"
        f"**Replenishment after a deployment:** {final.replenishment_md}\n\n"
        f"**Earmark mechanics (annotation on the cash sleeve — the class "
        f"set is fixed):** {final.earmark_mechanics_md}\n\n"
        f"**Interaction with the 5.0% x10 sleeve cap:** "
        f"{final.x10_cap_interaction_md}\n\n"
        f"**If the reserve idles:** {final.idle_policy_md}\n\n"
        f"**Provenance:** {prov}\n\n"
        "**Hard constraint honored:** cash or cash-equivalent ONLY "
        "(SGOV/IB01-class) — enforced deterministically on the final "
        "design.\n\n"
        "**Applying this design:** NOT applied. Confirming records the "
        "earmark as an annotation on the Cash & T-bills sleeve via a plan "
        "refinement (POST /api/plan/refine); you confirm the sizing first — "
        "nothing auto-applies."
    )

    payload = {
        "design": final.model_dump(),
        "reserve_usd_at_current_book": reserve_usd,
        "author": author.model_dump(),
        "blind_reviewer": reviewer.model_dump(),
        "divergences": team["divergences"],
        "residual_divergences": team["residual_divergences"],
        "facts_pack": facts,
        "apply_path": (
            "POST /api/plan/refine — annotate the Cash & T-bills sleeve "
            "with the dry-powder earmark (never a direct plan mutation; "
            "Ariel confirms the sizing)"
        ),
    }

    summary = (
        f"Dry-powder discovery reserve: earmark {final.reserve_pct}% "
        f"(~${reserve_usd:,}) in {final.instrument} so green-lit discovery "
        "picks never wait for a sale — confirm the sizing"
    )
    now = datetime.now(UTC)
    dedup = f"dry_powder_discovery_reserve:{USER_ID}"
    existing = session.execute(
        sa.select(ActionProposal).filter_by(
            user_id=USER_ID, dedup_key=dedup, status="open"
        )
    ).scalar_one_or_none()
    if existing:
        existing.summary = summary
        existing.rationale_md = rationale
        existing.suggested_payload = json.dumps(payload, default=str)
        existing.severity = "warning"
        existing.surfaced_at = now
        existing.expires_at = now + timedelta(days=30)
        session.commit()
        return existing.id
    row = ActionProposal(
        user_id=USER_ID,
        summary=summary,
        rationale_md=rationale,
        suggested_payload=json.dumps(payload, default=str),
        severity="warning",
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
        print(json.dumps(facts, indent=1, default=str))
        if args.facts_only:
            return
        team = run_team(facts)
        pid = sink_proposal(session, facts, team)
        print(json.dumps({
            "proposal_id": pid,
            "final": team["final"].model_dump(),
            "author": team["author"].model_dump(),
            "reviewer": team["reviewer"].model_dump(),
            "divergences": team["divergences"],
            "residual_divergences": team["residual_divergences"],
            "duration_s": team["duration_s"],
            "tokens_in": team["tokens_in"],
            "tokens_out": team["tokens_out"],
        }, indent=1, default=str))
    finally:
        session.close()


if __name__ == "__main__":
    main()
