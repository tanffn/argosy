"""LIVE end-to-end run of the team-sourced Alternatives sleeve subflow:
source -> deterministic verify -> ETP-aware fleet debate -> fund-manager decision.

No user-supplied tickers/size; nothing hardcoded; nothing promoted (this only
prints the team's AlternativesSleeveDecision draft). Confirms a hallucinated ISIN
is rejected and a registry-confirmed one verifies GREEN."""
import json

from argosy.orchestrator.flows.plan_synthesis.alternatives_phase import (
    run_alternatives_phase,
)

decision = run_alternatives_phase(
    user_id="ariel",
    macro_context={
        "anchor_sigma": 0.18,
        "regime": (
            "Israeli long-hold investor, heavily NVDA-concentrated (via RSUs) and "
            "actively deconcentrating; elevated US equity valuations; geopolitical "
            "risk elevated. Estate constraint: every instrument must be non-US."
        ),
    },
)

print("=== TEAM ALTERNATIVES SLEEVE DECISION ===")
print("decision:   ", decision.decision)
print("target_pct: ", decision.target_pct)
print("sleeve_sigma:", decision.sleeve_sigma)
print("rationale:  ", (decision.rationale_md or "")[:500])
print(f"\n--- {len(decision.instruments)} verified instruments ---")
for c in decision.instruments:
    v = c.verification
    print(f"  {c.symbol:<8} {c.asset_class:<16} dom={c.domicile:<3} isin={c.isin:<14} "
          f"w={c.weight_within_sleeve_pct:>5.1f}% [{v.severity}] verified={v.verified}")
print(f"\n--- {len(decision.violations)} violations / rejections ---")
for vmsg in decision.violations:
    print("  ", vmsg[:140])
print("\n=== full decision JSON (truncated) ===")
print(decision.model_dump_json(indent=2)[:1500])
