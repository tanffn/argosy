import sys; sys.path.insert(0, "tools/codex-tandem/scripts")
from pathlib import Path
from engine_codex import run_codex

PROMPT = """Adversarially review verify_instrument() in
argosy/services/instrument_verification.py. This is the gate that decides whether
a team-PROPOSED instrument is real + estate-clean enough to become a portfolio
holding. The threat model: an LLM agent hallucinates an instrument (fake ISIN,
wrong/forged domicile claim, a US-situs fund mislabeled as Irish). Nothing
unverified must EVER return verified=True.

Read verify_instrument, isin_is_valid, the registry loader, and
argosy/services/alternatives_types.py (VerificationResult/Evidence).

Find any path where:
1. A hallucinated or US-situs instrument reaches verified=True / GREEN.
2. The agent's claimed_domicile/claimed_isin can override an authoritative
   registry fact (it should be the reverse).
3. The coherence check (US prefix vs non-US domicile claim) can be bypassed.
4. A genuinely-clean, registry-confirmed instrument is wrongly rejected.
5. Edge cases: None/empty isin, mixed case, XS (international) prefix, an ISIN
   whose prefix is non-US but whose registry domicile is US, etc.

Give concrete inputs that break it if you find any. Output numbered findings with
severity (BLOCKER/HIGH/MEDIUM) + a one-line fix each.
"""

r = run_codex(
    node_dir=Path("D:/Projects/financial-advisor"),
    prompt=PROMPT, agent_name="verify_instrument_review_r2", role="reviewer",
    sandbox="danger-full-access", timeout_s=420,
)
out = Path("tmp_review/codex_verify_instrument_verdict_r2.txt")
out.write_text(r.verdict_text or "(no verdict)", encoding="utf-8")
print("WROTE", out)
print(r.verdict_text[:1400] if r.verdict_text else "(empty)")
