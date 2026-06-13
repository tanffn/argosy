import sys; sys.path.insert(0, "tools/codex-tandem/scripts")
from pathlib import Path
from engine_codex import run_codex

PROMPT = """Review the money-math in argosy/services/allocation_plan.py after the
Alternatives sleeve was changed from a hardcoded fixed 3% gold/BTC sleeve to a
TEAM-SOURCED supplied sleeve. Read build_target_allocation, derive_fi_weight,
_renormalise, _blended_sigma_for.

The contract that must hold:
1. build_target_allocation(alternatives_sleeve=None) reproduces the pre-sleeve
   baseline EXACTLY (no alternatives class, same FI/equity split as before the
   sleeve existed).
2. A supplied AlternativesSleeveDecision(target_pct=P, sleeve_sigma=S) is held as
   a FIXED policy weight: P is subtracted from the book BEFORE the six equity
   sleeves are renormalised at their agreed ratios; NVDA + FI are also fixed.
3. FI remains the SOLVER: derive_fi_weight finds the minimum FI such that the
   blended sigma <= the 0.18 anchor. The alternatives class's sigma in that blend
   must be the SOURCED S (pinned by label in _blended_sigma_for), NOT the fixed
   _SIGMA_BY_CLASS["alternatives"]=0.268. A higher S must force more FI.
4. Final weights sum to 100; blended_sigma <= anchor.

Find any bug: off-by-one in the subtract-before-renorm, double-counting the
sleeve, the label-pinned sigma not matching what _renormalise put under that
label, the no-sleeve path drifting from baseline, ratio_sum renorm errors, or the
anchor being breached for some (P,S) in P∈[0,4], S∈[0.10,0.70]. Output numbered
findings with severity + concrete (P,S) inputs that break it if any.
"""

r = run_codex(
    node_dir=Path("D:/Projects/financial-advisor"),
    prompt=PROMPT, agent_name="engine_sleeve_review_r2", role="reviewer",
    sandbox="danger-full-access", timeout_s=480,
)
out = Path("tmp_review/codex_engine_sleeve_verdict_r2.txt")
out.write_text(r.verdict_text or "(no verdict)", encoding="utf-8")
print("WROTE", out)
print(r.verdict_text[:1400] if r.verdict_text else "(empty)")
