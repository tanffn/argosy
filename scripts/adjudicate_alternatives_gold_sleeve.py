#!/usr/bin/env python3
"""Alternatives-sleeve GOLD adjudication — deterministic facts pack + LLM team.

The question (owner-raised, 2026-07-09): plan v73 carries a 3.11% Alternatives
sleeve, 100% IGLN (physical gold ETC) ~= $124k. The client's standing directive
(2026-07-06): gold is not banned but carries the BURDEN OF PROOF against
growth-bearing diversifiers; the fleet adjudicates on FI-date impact, never
holds gold by convention.

The trace found TWO conflicting records:
  * 2026-07-06 diversifier adjudication (author + blind re-derivation, model
    arithmetic): GOLD LOSES — negative geometric-growth net in every
    parameterization; sigma credit buys no solvency the book needs.
  * 2026-07-09 run-156 alternatives sub-fleet: approved 3% gold-only sleeve —
    but its candidate universe was alternatives-class ONLY (growth-bearing
    diversifiers were never in the running) and its case is qualitative (no
    FI-date arithmetic). The sleeve size (3%) is a hard-coded sourcing default.

Discipline (binding, mirrors scripts/adjudicate_nvda_glide_schedule.py):
  * Determinism supplies FACTS only: the book, plan targets, both prior records
    verbatim, sourced instrument look-throughs, the on-record model arithmetic.
  * The LLM TEAM adjudicates: author + BLIND reviewer (same raw facts, never
    the author's verdict), divergence compared IN CODE and forced through a
    reconciliation round.
  * The verdict lands as ONE needs-confirm inbox proposal (dedup-refreshed);
    NEVER auto-applied — if gold loses, applying it = /api/plan/refine
    redistribution that Ariel confirms.

Run:  .venv/Scripts/python.exe scripts/adjudicate_alternatives_gold_sleeve.py
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
from sqlalchemy.orm import sessionmaker  # noqa: E402

USER_ID = "ariel"
DB_URL = f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}"

MIN_HOLDING_USD = 4_000  # book block: aggregate the long tail below this


# ---------------------------------------------------------------------------
# Deterministic facts pack
# ---------------------------------------------------------------------------
def build_facts(session) -> dict:
    snap = session.execute(sa.text(
        "select id, snapshot_date, positions_json, totals_json, fx_usd_nis "
        "from portfolio_snapshots order by id desc limit 1"
    )).mappings().one()
    positions = json.loads(snap["positions_json"])
    totals = json.loads(snap["totals_json"])
    book_usd = float(totals["total_usd_value_k"]) * 1000.0

    holdings: dict[str, float] = {}
    for p in positions:
        sym = (p.get("symbol") or "?").strip() or "?"
        holdings[sym] = holdings.get(sym, 0.0) + (p.get("usd_value_k") or 0.0) * 1000.0
    small = {s: v for s, v in holdings.items() if v < MIN_HOLDING_USD}
    holdings = {s: v for s, v in holdings.items() if v >= MIN_HOLDING_USD}
    if small:
        holdings[f"(long tail, {len(small)} names)"] = sum(small.values())

    nvda_usd = holdings.get("NVDA", 0.0)
    nvda_weight = nvda_usd / book_usd * 100.0

    # Rough deterministic US-facing estimate: US single names + US-index
    # sleeves at 100%, global sleeves at their on-record US weights.
    _us_full = {"NVDA", "SCHD", "CSPX", "SGOV", "BRK/B", "GOOG", "CNDX",
                "AMD", "AMZN", "QQQM", "META", "RKT", "SOFI", "VTV", "SPMO",
                "VOO", "SCHG", "O", "SPMV", "FUSA", "TSLA", "CRM", "NOW",
                "IUHC", "SPCX", "BMY", "RXRX", "OKLO", "TEM", "NKE", "IWQU"}
    _us_partial = {"FWRA": 0.60, "ACWD": 0.60, "IWDP": 0.55, "IWQU": 0.67}
    us_usd = 0.0
    for s, v in holdings.items():
        if s in _us_partial:
            us_usd += v * _us_partial[s]
        elif s in _us_full:
            us_usd += v
    us_facing_pct = us_usd / book_usd * 100.0

    pv = session.execute(sa.text(
        "select id, version_label, target_allocation_json from plan_versions "
        "where role='current' order by id desc limit 1"
    )).mappings().one()
    ta = json.loads(pv["target_allocation_json"])
    plan_targets = {c["label"]: c["target_pct"] for c in ta["classes"]}
    by_label = {c["label"]: c for c in ta["classes"]}
    alt = by_label["Alternatives"]
    intl = by_label["International developed (ex-US)"]

    sleeve_pct = float(alt["target_pct"])
    sleeve_usd = sleeve_pct / 100.0 * book_usd

    # The two conflicting on-record decisions, quoted VERBATIM from the
    # current plan's own target_allocation_json (plan v{pv.id}).
    record_2026_07_06_gold_loses = intl["rationale"]
    record_2026_07_09_alt_fm_approves = alt["rationale"]
    record_igln_instrument = alt["instruments"][0]["rationale"]

    return {
        "as_of": str(snap["snapshot_date"]),
        "plan_version_id": pv["id"],
        "plan_version_label": pv["version_label"],
        "book": {
            "book_usd": round(book_usd),
            "nvda_lookthrough_pct": round(nvda_weight, 1),
            # direct NVDA only; fund look-through adds more — stated honestly
            "nvda_lookthrough_note": (
                "direct NVDA position only; index sleeves (CSPX/QQQM/CNDX/"
                "IWQU...) add fund-held NVDA on top"
            ),
            "us_facing_pct": round(us_facing_pct, 1),
            "holdings": {s: round(v) for s, v in holdings.items()},
        },
        "sleeve_on_trial": {
            "label": "Alternatives",
            "target_pct": sleeve_pct,
            "usd_at_current_book": round(sleeve_usd),
            "current_instrument": "IGLN (iShares Physical Gold ETC, "
                                  "IE00B4ND3602, Ireland, physically backed)",
            "sigma_class_used_by_engine": 0.16,
        },
        "standing_directive": (
            "Client (2026-07-06, binding): gold is NOT banned but carries the "
            "BURDEN OF PROOF vs growth-bearing diversifiers ('investing in a "
            "metal is lame'); the fleet adjudicates on FI-date impact and "
            "never recommends gold by convention."
        ),
        "owner_supplied_context": (
            "FI margin currently about -69k NIS/yr (thin/negative safe-FI "
            "margin): expected-return drag directly delays the earliest safe "
            "FI date; the client is long-hold and NVDA-concentrated with "
            "NVIDIA salary income."
        ),
        "prior_records_verbatim": {
            "2026-07-06_diversifier_adjudication_GOLD_LOSES":
                record_2026_07_06_gold_loses,
            "2026-07-09_run156_alternatives_fm_APPROVES_3pct_gold":
                record_2026_07_09_alt_fm_approves,
            "2026-07-09_run156_igln_instrument_rationale":
                record_igln_instrument,
        },
        "structural_note": (
            "The run-156 alternatives sub-fleet that approved the sleeve was "
            "scoped to alternatives-class candidates ONLY (sourcing default "
            "sleeve_pct=3.0 is hard-coded); growth-bearing diversifiers were "
            "never candidates in that phase, so gold never faced them there."
        ),
        "candidate_facts_provenance": (
            "look-through numbers below are the 2026-07-06 adjudication's "
            "sourced facts (justETF/issuer, 2026-05/06) as recorded in the "
            "current plan; gold return history per UBS/CS Global Investment "
            "Returns Yearbook 1900-2023 (~0.7%/yr real) and Erb & Harvey "
            "2013 (~0-1%/yr real); MC real equity base 5.0%/yr."
        ),
    }


def facts_to_candidates(facts: dict) -> list[dict]:
    """Candidate table for the adjudicator — incumbent gold + the
    growth-bearing decorrelators already sourced on the record."""
    return [
        {
            "symbol": "IGLN", "name": "iShares Physical Gold ETC (incumbent)",
            "isin": "IE00B4ND3602", "domicile": "Ireland (ETC, non-US-situs)",
            "nvda_weight_pct": 0.0, "us_weight_pct": 0.0, "yield_pct": 0.0,
            "character": "physical gold; sigma 0.16 (engine); long-run real "
                         "return ~0.7-1.0%/yr (DMS Yearbook / Erb & Harvey); "
                         "rho to equity 0.25 framework / ~0.0 long-run",
            "notes": "monetary/geopolitical ballast; no yield, no growth; "
                     "Israeli CGT wrapper treatment of a commodity ETC "
                     "unresolved (may lose CPI-real-gain relief)",
        },
        {
            "symbol": "EXUS", "name": "Xtrackers MSCI World ex USA (deepen "
                                      "existing sleeve)",
            "domicile": "Ireland UCITS",
            "nvda_weight_pct": 0.0, "us_weight_pct": 0.0,
            "character": "growth-bearing developed ex-US equity; EUR/JPY/GBP "
                         "revenue; sigma 0.20 (engine); won the 2026-07-06 "
                         "adjudication",
            "notes": "already a 14.3% plan sleeve — this candidate = deepen "
                     "it by the sleeve amount",
        },
        {
            "symbol": "EIMI", "name": "iShares Core MSCI EM IMI (deepen)",
            "domicile": "Ireland UCITS",
            "nvda_weight_pct": 0.0, "us_weight_pct": 0.0,
            "character": "growth-bearing EM equity",
            "notes": "~48% Taiwan+Korea AI-supply-chain overlap with the NVDA "
                     "factor (on-record look-through) — weak decorrelator",
        },
        {
            "symbol": "INFR", "name": "iShares Global Infrastructure UCITS",
            "domicile": "Ireland UCITS",
            "us_weight_pct": 62.6,
            "character": "growth-bearing real-asset equity, yield-rich",
            "notes": "62.6% US look-through (on-record) — partially re-buys "
                     "the US complex",
        },
        {
            "symbol": "WSML", "name": "iShares MSCI World Small Cap UCITS",
            "domicile": "Ireland UCITS",
            "us_weight_pct": 51.5,
            "character": "growth-bearing global small-cap",
            "notes": "51.5% US look-through (on-record)",
        },
        {
            "symbol": "IWDP", "name": "iShares Developed Markets Property "
                                      "Yield UCITS (already held $34k)",
            "domicile": "Ireland UCITS",
            "character": "growth-bearing global REIT; rate-sensitive, "
                         "moderately decorrelated from the NVDA factor",
            "notes": "plan also carries a Real assets (REIT/TIPS) sleeve at "
                     "2.02% (DPYA primary)",
        },
    ]


def evidence_md(facts: dict) -> str:
    pr = facts["prior_records_verbatim"]
    return (
        "## The sleeve on trial\n"
        f"{json.dumps(facts['sleeve_on_trial'], indent=1)}\n\n"
        "## The client's standing directive (BINDING)\n"
        f"{facts['standing_directive']}\n\n"
        "## Owner-supplied context\n"
        f"{facts['owner_supplied_context']}\n\n"
        "## PRIOR RECORD 1 — 2026-07-06 diversifier adjudication "
        "(author + blind re-derivation, model arithmetic): GOLD LOSES\n"
        f"{pr['2026-07-06_diversifier_adjudication_GOLD_LOSES']}\n\n"
        "## PRIOR RECORD 2 — 2026-07-09 run-156 alternatives fund manager: "
        "APPROVE 3% gold-only sleeve (qualitative; alternatives-only "
        "candidate universe)\n"
        f"{pr['2026-07-09_run156_alternatives_fm_APPROVES_3pct_gold']}\n\n"
        "### run-156 IGLN instrument rationale\n"
        f"{pr['2026-07-09_run156_igln_instrument_rationale']}\n\n"
        "## Structural note\n"
        f"{facts['structural_note']}\n\n"
        "## Provenance of candidate numbers\n"
        f"{facts['candidate_facts_provenance']}\n\n"
        "## YOUR TASK (precise)\n"
        "Adjudicate whether the EXISTING 3.11% (~$124k) gold sleeve beats "
        "redeploying the SAME 3.11% into growth-bearing NVDA-decorrelated "
        "diversifiers, on EARLIEST-SAFE-FI-DATE impact for THIS client. "
        "Burden of proof is ON GOLD. You must explicitly engage BOTH prior "
        "records: if you keep gold, defeat Record 1's geometric-drag "
        "arithmetic on its own terms; if you remove gold, address Record 2's "
        "ballast/orthogonality case. gold_wins=false means: recommend "
        "redistributing the sleeve via a plan refinement (the client "
        "confirms; nothing auto-applies)."
    )


# ---------------------------------------------------------------------------
# Fleet run — author + blind reviewer + code-forced reconciliation
# ---------------------------------------------------------------------------
def _divergences(a, b) -> list[str]:
    out = []
    if a.gold_wins != b.gold_wins:
        out.append(f"gold_wins: author={a.gold_wins} vs reviewer={b.gold_wins}")
    if a.chosen_symbol.upper() != b.chosen_symbol.upper():
        out.append(f"chosen_symbol: author={a.chosen_symbol} vs "
                   f"reviewer={b.chosen_symbol}")
    if abs(a.sleeve_pct - b.sleeve_pct) > 1.0:
        out.append(f"sleeve_pct: author={a.sleeve_pct} vs "
                   f"reviewer={b.sleeve_pct}")
    return out


def run_team(facts: dict) -> dict:
    from argosy.agents.plan_change_team import DiversifierAdjudicatorAgent
    from argosy.services.fleet_reliability import (
        FleetRetryConfig,
        call_reliably_sync,
    )

    candidates = facts_to_candidates(facts)
    ev = evidence_md(facts)
    book = facts["book"]
    plan_targets = facts.get("plan_targets") or {}

    cfg = FleetRetryConfig(hard_timeout_s=600.0)
    totals = {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    t0 = time.monotonic()

    def _run(scope: str, *, extra_evidence: str = "", blind: bool = False):
        def _attempt():
            agent = DiversifierAdjudicatorAgent(user_id=USER_ID)
            return agent.run_sync(
                candidates=candidates,
                evidence_md=ev + extra_evidence,
                book=book,
                plan_targets=plan_targets,
                blind_rederive=blind,
            )
        rep = call_reliably_sync(_attempt, scope=scope, config=cfg)
        totals["cost_usd"] += float(getattr(rep, "cost_usd", 0) or 0)
        totals["tokens_in"] += int(getattr(rep, "tokens_in", 0) or 0)
        totals["tokens_out"] += int(getattr(rep, "tokens_out", 0) or 0)
        return rep.output

    print("[team] author adjudicating ...", flush=True)
    author = _run("alt_gold_author")
    print(f"[team] author: gold_wins={author.gold_wins} -> "
          f"{author.chosen_symbol} @ {author.sleeve_pct}%", flush=True)

    print("[team] blind reviewer re-deriving ...", flush=True)
    reviewer = _run("alt_gold_blind_reviewer", blind=True)
    print(f"[team] reviewer: gold_wins={reviewer.gold_wins} -> "
          f"{reviewer.chosen_symbol} @ {reviewer.sleeve_pct}%", flush=True)

    divergences = _divergences(author, reviewer)
    final, reconciled = author, False
    if divergences:
        print(f"[team] DIVERGENCE ({len(divergences)}) — reconciliation round",
              flush=True)
        extra = (
            "\n\n## RECONCILIATION ROUND (code-forced)\n"
            "Two independent derivations diverged:\n"
            + "\n".join(f"- {d}" for d in divergences)
            + "\n\nAuthor verdict JSON:\n" + author.model_dump_json(indent=1)
            + "\n\nBlind reviewer verdict JSON:\n"
            + reviewer.model_dump_json(indent=1)
            + "\n\nWeigh both, decide, and record which argument won and why."
        )
        final = _run("alt_gold_reconcile", extra_evidence=extra)
        reconciled = True
        print(f"[team] reconciled final: gold_wins={final.gold_wins} -> "
              f"{final.chosen_symbol} @ {final.sleeve_pct}%", flush=True)

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
    sleeve = facts["sleeve_on_trial"]

    prov = (
        f"Author adjudicated gold_wins={author.gold_wins} "
        f"({author.chosen_symbol} @ {author.sleeve_pct}%); blind reviewer "
        f"independently re-derived gold_wins={reviewer.gold_wins} "
        f"({reviewer.chosen_symbol} @ {reviewer.sleeve_pct}%)."
    )
    if team["divergences"]:
        prov += (
            " Divergence was code-forced through a reconciliation round: "
            + "; ".join(team["divergences"])
            + f". Final (reconciled): gold_wins={final.gold_wins}, "
            f"{final.chosen_symbol} @ {final.sleeve_pct}%."
        )
        if team["residual_divergences"]:
            prov += (" RESIDUAL divergence remains: "
                     + "; ".join(team["residual_divergences"]))
    else:
        prov += " The two blind derivations AGREED."

    if final.gold_wins:
        headline = (
            f"GOLD STANDS on the merits: keep the Alternatives sleeve at "
            f"~{final.sleeve_pct}% ({final.chosen_symbol}). The merits case "
            "is now ON RECORD (it previously was not)."
        )
        apply_note = (
            "Nothing changes; confirming records the sleeve as a deliberate, "
            "merits-adjudicated decision instead of a sourcing-default."
        )
    else:
        headline = (
            f"GOLD LOSES the burden-of-proof adjudication: redistribute the "
            f"{sleeve['target_pct']}% (~${sleeve['usd_at_current_book']:,}) "
            f"Alternatives sleeve into {final.chosen_symbol} "
            f"({final.chosen_name or 'growth-bearing diversifier'})."
        )
        apply_note = (
            "NOT applied. Applying = a plan refinement (POST /api/plan/refine) "
            "moving the Alternatives target to the chosen growth-bearing "
            "sleeve; you confirm first. "
            + (f"Funding/redistribution note: {final.funding_note}"
               if final.funding_note else "")
        )

    rationale = (
        "## Alternatives-sleeve gold adjudication — fleet verdict "
        "(needs your confirmation)\n\n"
        f"**Verdict: {headline}**\n\n"
        f"**Gold verdict (merits, burden of proof on gold):**\n"
        f"{final.gold_verdict_md}\n\n"
        f"**Fleet rationale:** {final.rationale}\n\n"
        f"**Provenance:** {prov}\n\n"
        "**Why this adjudication ran:** the trace found the v73 sleeve never "
        "won a merits case against growth-bearing diversifiers — the run-156 "
        "alternatives sub-fleet that approved it only ever compared "
        "alternatives-class candidates (3% sleeve size is a sourcing "
        "default), while the one on-record arithmetic adjudication "
        "(2026-07-06) concluded gold LOSES. This run put both records in "
        "front of an author + blind reviewer.\n\n"
        f"**Applying this verdict:** {apply_note}"
    )

    payload = {
        "verdict": final.model_dump(),
        "author": author.model_dump(),
        "blind_reviewer": reviewer.model_dump(),
        "divergences": team["divergences"],
        "residual_divergences": team["residual_divergences"],
        "facts_pack": facts,
        "apply_path": (
            "keep sleeve (record merits case)" if final.gold_wins else
            "POST /api/plan/refine — move Alternatives 3.11% to the chosen "
            "growth-bearing sleeve (never a direct plan mutation; Ariel "
            "confirms)"
        ),
    }

    severity = "info" if final.gold_wins else "warning"
    dedup = f"plan_alternatives_gold_sleeve_verdict:{USER_ID}"
    summary = (
        "Alternatives sleeve (3.11% gold/IGLN, ~$124k) adjudicated on the "
        "merits: "
        + ("gold STANDS — confirm to record the merits case"
           if final.gold_wins else
           f"gold LOSES — confirm redistribution to {final.chosen_symbol}")
    )
    now = datetime.now(UTC)
    existing = session.execute(
        sa.select(ActionProposal).filter_by(
            user_id=USER_ID, dedup_key=dedup, status="open"
        )
    ).scalar_one_or_none()
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
        # thread real plan targets into the team prompt
        pv = session.execute(sa.text(
            "select target_allocation_json from plan_versions "
            "where role='current' order by id desc limit 1"
        )).mappings().one()
        facts["plan_targets"] = {
            c["label"]: c["target_pct"]
            for c in json.loads(pv["target_allocation_json"])["classes"]
        }
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
