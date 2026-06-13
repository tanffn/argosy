import sys; sys.path.insert(0, "tools/codex-tandem/scripts")
from pathlib import Path
from engine_codex import run_codex
PROMPT = """Read argosy/services/allocation_plan.py functions _renormalise,
derive_fi_weight, _blended_sigma_for, build_target_allocation. The Alternatives
sleeve is now a SUPPLIED AlternativesSleeveDecision (not hardcoded). Confirm or
break: (1) alternatives_sleeve=None reproduces the no-sleeve baseline; (2) a
supplied target_pct P is subtracted before the 6 equity sleeves renormalise;
(3) derive_fi_weight uses the SOURCED sleeve_sigma (pinned by label in
_blended_sigma_for), so higher sigma -> more FI; (4) blended_sigma <= 0.18 anchor
holds. Give numbered findings + concrete (P, sigma) breakers if any. Be concise."""
r = run_codex(node_dir=Path("D:/Projects/financial-advisor"), prompt=PROMPT,
              agent_name="engine_quick", role="reviewer",
              sandbox="danger-full-access", timeout_s=300)
Path("tmp_review/codex_engine_quick_verdict.txt").write_text(r.verdict_text or "(no verdict)", encoding="utf-8")
print("LEN", len(r.verdict_text or ""))
print(r.verdict_text[:1800] if r.verdict_text else "(empty)")
