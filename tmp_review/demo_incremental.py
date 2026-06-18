"""Real-data demonstration of the incremental-plan capstone (Task 4).

Builds the base derivation graph for ``ariel`` from the LIVE db (read-only),
constructs change-requests from the outstanding reader/codex findings against
the latest blocked draft, runs ``run_incremental_cycle``, and writes a UTF-8
report to tmp_review/demo_incremental_report.txt.

The ladder participants here are BOUNDED DETERMINISTIC doubles (no real
claude.exe call) so the demo is reproducible and cannot hang on an unstable
backend — the plan permits real participants but requires they be bounded; a
deterministic double is the safe choice for a repeatable demonstration.

UTF-8 report only — NEVER print the shekel sign to the cp1252 console (the
stdout echo is ascii-replace encoded).

Usage:  .venv/Scripts/python.exe tmp_review/demo_incremental.py [decision_run_id]
"""
from __future__ import annotations

import os
import sys

OUT = "tmp_review/demo_incremental_report.txt"
USER_ID = "ariel"


# Deterministic, bounded ladder participants (the LLM seam) ------------------ #
class _DemoParticipants:
    """B concedes recipe changes it agrees with; otherwise the arbiter escalates
    genuine judgment calls to the user. Deterministic — no LLM call."""

    def __init__(self, escalate: bool) -> None:
        self._escalate = escalate

    def peer_round(self, *, change, prior_turns, round):
        from argosy.orchestrator.flows.negotiation_ladder import PeerVerdict
        if self._escalate:
            return PeerVerdict.UNRESOLVED, "this needs the client's call"
        return PeerVerdict.B_CONCEDES, "agreed — evidence supports the change"

    def arbiter(self, *, change, prior_turns):
        from argosy.orchestrator.flows.negotiation_ladder import ArbiterClass
        return (
            ArbiterClass.GENUINE_DECISION,
            "a values judgment for the client (risk tolerance / SWR)",
        )


def main() -> None:
    os.environ["ARGOSY_INCREMENTAL_PLAN"] = "1"

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings
    from argosy.state.models import PlanVersion
    from argosy.quality.change_adjudication import (
        Author, AuthorKind, ChangeKind, ChangeRequest,
    )
    from argosy.orchestrator.flows.incremental_plan import (
        build_base_graph, run_incremental_cycle,
    )

    url = get_settings().database_url.replace("+aiosqlite", "")
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    out: list[str] = []

    def w(s: str = "") -> None:
        out.append(s)

    with Session() as session:
        if len(sys.argv) > 1:
            rid = int(sys.argv[1])
        else:
            pv = session.execute(
                select(PlanVersion)
                .where(PlanVersion.user_id == USER_ID)
                .where(PlanVersion.role.in_(("draft", "current")))
                .order_by(PlanVersion.id.desc())
            ).scalars().first()
            rid = pv.decision_run_id if pv else 0

        w(f"=== incremental-plan demo — user={USER_ID} decision_run_id={rid} ===")

        # 1) Base graph from live data (read-only; resolver_values=None ->
        #    sourced from resolve_plan_numbers, the authoritative manifest).
        graph = build_base_graph(session, USER_ID, decision_run_id=rid)
        w("\n--- base graph (canonical scalars, resolver-sourced) ---")
        for key in ("retirement.fi_margin_signed_nis",
                    "retirement.earliest_safe_age",
                    "portfolio.liquid_net_worth_nis",
                    "concentration.us_situs_estate_nis",
                    "concentration.nvda_current_pct"):
            try:
                w(f"  {key} = {graph.get(key).value!r}")
            except Exception as exc:  # noqa: BLE001
                w(f"  {key} = <absent: {exc!r}>")

        # The per-symbol US-situs list (collection-derived).
        w("\n--- US-situs per-symbol breakdown ---")
        try:
            breakdown = graph.get("concentration.us_situs_symbol_breakdown").value or []
            for r in sorted(breakdown, key=lambda x: -float(x.get("usd_value") or 0.0)):
                w(f"  {r['classification']:<9} {r['symbol'] or '(no symbol)':<8} "
                  f"USD {float(r.get('usd_value') or 0.0):,.0f}  {r.get('name','')}")
        except Exception as exc:  # noqa: BLE001
            w(f"  <breakdown absent: {exc!r}>")

        # Canonical surfaces — proof the FI verdict + tile agree, age agrees.
        w("\n--- canonical surfaces (one node -> many surfaces) ---")
        for skey in ("surface:fi_verdict", "surface:dashboard.fi_tile",
                     "surface:retirement_age_headline", "surface:dashboard.age_tile",
                     "surface:us_situs_estate_headline"):
            try:
                w(f"  {skey}: {graph.get(skey).value}")
            except Exception as exc:  # noqa: BLE001
                w(f"  {skey}: <absent: {exc!r}>")

        # 2) Change-requests from outstanding findings.
        #    (a) An evidence-resolvable FX correction (input) -> applies.
        #    (b) A genuine SWR/risk policy change (recipe) -> escalates to user.
        crs = [
            ChangeRequest(
                target_node_key="fx.usd_nis",
                author=Author(AuthorKind.AGENT, "fund_manager"),
                kind=ChangeKind.SET_INPUT,
                payload={"value": graph.get("fx.usd_nis").value},
                rationale="reconcile FX to the snapshot BOI rate (no-op smoke)",
            ),
            ChangeRequest(
                target_node_key="retirement.required_real_yield_pct",
                author=Author(AuthorKind.AGENT, "fund_manager"),
                kind=ChangeKind.SET_RECIPE,
                payload={"value": 0.05},
                rationale="raise the SWR assumption above the conservative default",
            ),
        ]

        res = run_incremental_cycle(
            session, user_id=USER_ID, decision_run_id=rid,
            change_requests=crs, participants=_DemoParticipants(escalate=True),
            persist=False,
            recipe_node_keys={"retirement.required_real_yield_pct"},
        )

        w("\n--- cycle result ---")
        w(f"  closed       = {res.closed}")
        w(f"  promotable   = {res.promotable}  (no authority set supplied -> fail-closed)")
        w(f"  recomputed   = {res.recomputed}")
        w(f"  open_flags   = {res.open_flags}")
        w("\n  real client questions (arbiter-certified, finite):")
        if res.real_questions:
            for q in res.real_questions:
                w(f"    - [{q['target_node_key']}] {q['question']}")
        else:
            w("    (none — cycle resolved without escalation)")

        # Post-cycle surfaces — show FI verdict + tile still byte-identical.
        if res.graph is not None:
            v = res.graph.get("surface:fi_verdict").value
            t = res.graph.get("surface:dashboard.fi_tile").value
            w("\n--- post-cycle FI cross-surface consistency ---")
            w(f"  fi_verdict == fi_tile : {v == t}")
            w(f"  fi_verdict           : {v}")
            w(f"  fi_tile              : {t}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out).encode("ascii", "replace").decode("ascii"))
    print(f"\n[written to {OUT}]")


if __name__ == "__main__":
    main()
