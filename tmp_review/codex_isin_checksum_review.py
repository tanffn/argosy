import sys; sys.path.insert(0, "tools/codex-tandem/scripts")
from pathlib import Path
from engine_codex import run_codex

PROMPT = """Review the ISO 6166 ISIN check-digit + structural validator in
argosy/services/instrument_verification.py (functions isin_is_valid,
isin_country_prefix). This is a CORRECTNESS GATE: a False-positive (a fabricated
ISIN scored valid) could let a hallucinated instrument become a portfolio holding.

Verify against the ISO 6166 spec:
1. Letter->digit expansion A=10..Z=35, then Luhn mod-10 over the digit string,
   doubling every second digit FROM THE RIGHT. Is the parity/direction correct?
2. Structural checks (length 12, 2-alpha prefix, 9 alnum body, trailing check
   digit). Any way a malformed string slips through or a valid one is rejected?
3. Try to construct an ISIN the implementation MIS-SCORES (valid scored invalid,
   or invalid scored valid). Give concrete example ISINs with your hand-computed
   check digit if you find one.
4. Is restricting the country prefix to a fixed allowlist a correctness risk
   (rejecting legitimate ISINs from other domiciles)? It is intentional scoping
   for this single book — comment only if it breaks a check that should pass.

Output numbered findings with severity, and concrete mis-scored ISIN examples if any.
"""

r = run_codex(
    node_dir=Path("D:/Projects/financial-advisor"),
    prompt=PROMPT, agent_name="isin_checksum_review", role="reviewer",
    sandbox="danger-full-access", timeout_s=420,
)
out = Path("tmp_review/codex_isin_checksum_verdict.txt")
out.write_text(r.verdict_text or "(no verdict)", encoding="utf-8")
print("WROTE", out)
print(r.verdict_text[:1200] if r.verdict_text else "(empty)")
