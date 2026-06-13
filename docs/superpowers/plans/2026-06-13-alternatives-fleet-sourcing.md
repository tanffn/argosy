# Alternatives Sleeve — Fleet-Sourced, Verified, Synthesis-Integrated — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Alternatives sleeve fully team-sourced, deterministically verified (no hallucinated instrument can become a holding), debated by an alternatives-aware agent fleet, and threaded into the canonical plan as a draft — with **nothing hardcoded** (size AND instruments are team-derived; 0% is a valid team answer).

**Architecture:** A new alternatives subflow runs as a **phase inside `run_synthesis()`** (not a bolt-on, per codex E2E verdict #5): `source (AlternativesSourcerAgent) → deterministic verify (InstrumentVerificationService) → estate gate → alternatives-aware fleet debate → fund-manager decision (AlternativesSleeveDecision, target_pct may be 0) → threaded into target_allocation_json before resolve_target_allocation_json()`. The deterministic engine consumes a *supplied* sleeve (default `None`); the hardcoded `_ALTERNATIVES_SLEEVE` / `ALTERNATIVES_TARGET_PCT` are removed. Sleeve sigma is computed from the verified instruments, not a fixed 0.268.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy, FastAPI, pytest. Agents run on the `claude_code` backend (no API key) via `BaseAgent.run_sync`. Codex-tandem reviews every money-math slice.

---

## Binding principle (do not violate)

The agent team sources + verifies + sizes the sleeve. User supplies no tickers, no size, no tilts. Every instrument is estate-gated (non-US) AND deterministically verified (real ISIN, real domicile, tradeable). `0%` is a legitimate team outcome. Frozen constants are *seeds for verification*, never authority over what the team may pick. Promotion to `role="current"` is always the user's act — this plan only produces a `role="draft"`.

## Spec source

Codex E2E design verdict (`tmp_review/codex_alternatives_e2e_verdict.txt`), 8 findings + 10-step build sequence. This plan implements that sequence. Methodology verdict (`tmp_review/codex_alternatives_verdict.txt`) supplies the sigma math primitives (gold σ=0.16, BTC σ=0.70, linear blend).

## File structure

**Create:**
- `argosy/services/instrument_verification.py` — deterministic `InstrumentVerificationService` (ISIN checksum, country-prefix↔domicile coherence, registry lookup, optional tradeability cross-check). Pure-by-default; network only for the optional cross-check.
- `argosy/data/verified_instruments.yaml` — verified-facts registry (cache, seeded from real issuer factsheets). Authoritative source of record per instrument: ISIN, domicile, product_type, exchange, source_url, verified_on.
- `argosy/agents/alternatives_reviewers.py` — alternatives-aware reviewer agents (exposure/structure, macro/diversification, risk/liquidity/tax) + alternatives FM/synthesizer. Reuse `AgentReport` / `ConfidenceBand`; do NOT reuse `TraderProposal`.
- `argosy/orchestrator/flows/plan_synthesis/alternatives_phase.py` — the subflow: source → verify → gate → debate → decide → `AlternativesSleeveDecision`.
- `argosy/services/alternatives_types.py` — `AlternativesSleeveDecision`, `VerifiedAlternativesCandidate`, `VerificationEvidence`, `VerificationResult` models.

**Modify:**
- `argosy/services/allocation_plan.py` — remove `_ALTERNATIVES_SLEEVE`, `ALTERNATIVES_TARGET_PCT`/`_MAX_PCT`/`_GOLD_FRAC`/`_BTC_FRAC`; add `alternatives_sleeve: AlternativesSleeveDecision | None = None` param threaded through `derive_fi_weight` / `_renormalise` / `build_target_allocation`.
- `argosy/services/alternatives_sourcing.py` — replace `gate_proposal` with `verify_and_gate_proposal` (source → verify → estate-gate → cap/weight checks → `VerifiedAlternativesCandidate[]`).
- `argosy/services/retirement/sigma_calibration.py` — make alternatives sigma computed from instrument weights, not fixed `0.268`.
- `argosy/services/sigma_glidepath.py` — keep the alternatives needle but drive it from the supplied sleeve sigma.
- `argosy/services/target_allocation_doc.py` — `resolve_target_allocation_json` must NOT carry forward a stale alternatives sleeve when fresh verification fails (codex risk #6): build explicit `0%` with provenance instead.
- `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` — call the alternatives phase before `resolve_target_allocation_json`; thread the decision in; persist `agent_reports` + a `decision_phases` row for the alternatives decision.
- `argosy/agents/alternatives_sourcer.py` — drop the `sleeve_pct` target nudge from the user prompt (team derives size from zero); bump default model to `claude-opus-4-8`.
- `tests/test_allocation_plan.py` — replace the hardcoded-3%-sleeve assertions (line ~219) with the three-fixture model (verified 3% / verified 0% / no-sleeve).

---

## Phase C — Deterministic instrument verification (highest risk)

### Task C1: `VerificationResult` / evidence types

**Files:**
- Create: `argosy/services/alternatives_types.py`
- Test: `tests/test_alternatives_types.py`

- [ ] **Step 1: Write the failing test**

```python
from argosy.services.alternatives_types import (
    VerificationEvidence, VerificationResult, VerifiedAlternativesCandidate,
)

def test_verification_result_pass_requires_evidence():
    ev = VerificationEvidence(
        isin_checksum_ok=True, isin_prefix="IE", domicile_coherent=True,
        registry_hit=True, tradeable=None, source_url="https://issuer/factsheet",
    )
    r = VerificationResult(symbol="SGLD", verified=True, severity="GREEN",
                           reason="registry + checksum ok", evidence=ev)
    assert r.verified and r.severity == "GREEN"

def test_verification_result_reject_is_not_verified():
    ev = VerificationEvidence(isin_checksum_ok=False, isin_prefix="US",
                              domicile_coherent=False, registry_hit=False,
                              tradeable=None, source_url=None)
    r = VerificationResult(symbol="FAKE", verified=False, severity="RED",
                           reason="bad checksum + US prefix", evidence=ev)
    assert not r.verified and r.severity == "RED"
```

- [ ] **Step 2: Run test to verify it fails** — `…pytest tests/test_alternatives_types.py -q` → ImportError.

- [ ] **Step 3: Write the models**

```python
"""Typed objects for the team-sourced, verified Alternatives sleeve."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field

class VerificationEvidence(BaseModel):
    isin_checksum_ok: bool
    isin_prefix: str | None = None          # first 2 chars of the ISIN (issuing country)
    domicile_coherent: bool                 # claimed domicile not contradicted by ISIN prefix
    registry_hit: bool                      # found in verified_instruments.yaml
    tradeable: bool | None = None           # optional yfinance cross-check; None = not checked
    source_url: str | None = None           # authoritative factsheet / ISIN-registry URL

class VerificationResult(BaseModel):
    symbol: str
    verified: bool
    severity: Literal["GREEN", "YELLOW", "RED"]
    reason: str
    evidence: VerificationEvidence

class VerifiedAlternativesCandidate(BaseModel):
    symbol: str
    name: str
    asset_class: str
    domicile: str
    isin: str
    weight_within_sleeve_pct: float
    conviction: Literal["HIGH", "MEDIUM", "LOW"]
    thesis_md: str
    verification: VerificationResult

class AlternativesSleeveDecision(BaseModel):
    target_pct: float = Field(ge=0.0, description="Final sleeve % of book; 0 is valid.")
    sleeve_sigma: float = Field(ge=0.0, description="Computed from verified instruments.")
    instruments: list[VerifiedAlternativesCandidate] = Field(default_factory=list)
    decision: Literal["approve", "cut", "0_percent", "insufficient_data"]
    rationale_md: str
    review_summary_md: str = ""
    violations: list[str] = Field(default_factory=list)

    def model_post_init(self, __ctx) -> None:
        if self.target_pct == 0 and self.instruments:
            raise ValueError("0% sleeve must carry no instruments")
        if self.target_pct > 0 and not self.instruments:
            raise ValueError("non-zero sleeve must carry instruments")
```

- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Commit** — `feat(alternatives): typed sleeve-decision + verification models`.

### Task C2: ISIN ISO-6166 checksum + structural validator

**Files:**
- Create: `argosy/services/instrument_verification.py`
- Test: `tests/test_instrument_verification.py`

- [ ] **Step 1: Write the failing test** (real ISINs; SGLD = `IE00B579F325`, an Invesco Physical Gold ETC).

```python
from argosy.services.instrument_verification import isin_is_valid, isin_country_prefix

def test_real_isin_passes_checksum():
    assert isin_is_valid("IE00B579F325") is True      # Invesco Physical Gold ETC (SGLD)
    assert isin_is_valid("US67066G1040") is True       # NVDA (valid ISIN, US prefix)

def test_fabricated_isin_fails_checksum():
    assert isin_is_valid("IE00B579F320") is False      # wrong check digit
    assert isin_is_valid("IE00B579F32") is False        # too short
    assert isin_is_valid("ZZ00B579F325") is False       # implausible country code

def test_country_prefix():
    assert isin_country_prefix("IE00B579F325") == "IE"
    assert isin_country_prefix("bad") is None
```

- [ ] **Step 2: Run to verify fail** — ImportError.

- [ ] **Step 3: Implement the checksum** (ISO 6166: expand letters to two digits A=10…Z=35, then Luhn from the right).

```python
"""Deterministic instrument verification for the Alternatives sleeve.

The agent team PROPOSES instruments; this service establishes, with no trust in
the agent's claims, that each pick is a REAL, tradeable, non-US-domiciled
security before it can become a holding. Deterministic core (ISIN checksum +
country-prefix↔domicile coherence + verified-facts registry) needs no network;
an OPTIONAL yfinance cross-check confirms tradeability when coverage exists.

Doctrine: the registry verifies FACTS about whatever the team picks; it is NOT an
allow-list that constrains the candidate universe. An instrument absent from the
registry is not forbidden — it is UNVERIFIED, and an unverified instrument can
never become a holding until its facts are confirmed against an authoritative
source. Frozen entries are seeds for verification, not authority over picks.
"""
from __future__ import annotations
import string

_ISO_COUNTRY_PREFIXES = frozenset({
    "IE", "LU", "DE", "FR", "GB", "JE", "GG", "CH", "NL", "US", "CA", "IL",
    "XS",  # Euroclear/Clearstream international (common for ETPs)
})

def isin_country_prefix(isin: str | None) -> str | None:
    if not isin or len(isin) != 12:
        return None
    p = isin[:2].upper()
    return p if p.isalpha() else None

def isin_is_valid(isin: str | None) -> bool:
    """True iff `isin` is structurally valid ISO 6166 with a correct check digit
    and a plausible country prefix."""
    if not isin or len(isin) != 12:
        return False
    s = isin.upper()
    if not (s[:2].isalpha() and s[2:11].isalnum() and s[11].isdigit()):
        return False
    if s[:2] not in _ISO_COUNTRY_PREFIXES:
        return False
    # expand letters → digits
    digits: list[int] = []
    for ch in s:
        if ch.isdigit():
            digits.append(int(ch))
        else:
            v = 10 + string.ascii_uppercase.index(ch)
            digits.append(v // 10)
            digits.append(v % 10)
    # Luhn from the right
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
```

- [ ] **Step 4: Run to verify pass.** If a chosen literal ISIN's real check digit differs, fix the *test literal* to the genuine ISIN (verify against an issuer factsheet), never weaken the algorithm.
- [ ] **Step 5: Codex-tandem review the checksum** (`tmp_review/codex_isin_checksum_review.py`, prompt: "verify this ISO-6166 ISIN check-digit implementation against the spec; try to find an ISIN it mis-scores"). Apply blockers.
- [ ] **Step 6: Commit** — `feat(alternatives): deterministic ISIN ISO-6166 validator`.

### Task C3: verified-facts registry + loader

**Files:**
- Create: `argosy/data/verified_instruments.yaml`
- Modify: `argosy/services/instrument_verification.py`
- Test: `tests/test_instrument_verification.py`

- [ ] **Step 1: Write the failing test**

```python
from argosy.services.instrument_verification import load_registry, registry_lookup

def test_registry_lookup_known_instrument():
    reg = load_registry()
    hit = registry_lookup("SGLD", reg)
    assert hit is not None and hit["domicile"] == "IE" and hit["source_url"]

def test_registry_lookup_unknown_returns_none():
    assert registry_lookup("TOTALLY_MADE_UP", load_registry()) is None
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Seed the registry** (only instruments verified against a real issuer factsheet TODAY; each carries its source URL — these are *cache entries*, re-checkable, not authority). Seed the ones the live sourcer already returned plus the methodology picks:

```yaml
# Verified-facts cache for the Alternatives sleeve. Each entry is FACTS about a
# real instrument, confirmed against the cited source_url. NOT an allow-list:
# the team may propose anything; only verified facts let a pick become a holding.
SGLD:
  name: Invesco Physical Gold ETC
  isin: IE00B579F325
  domicile: IE
  product_type: ETC
  exchange: LSE
  source_url: https://www.invesco.com/uk/en/financial-products/etfs/invesco-physical-gold-etc.html
  verified_on: 2026-06-13
IGLN:
  name: iShares Physical Gold ETC
  isin: IE00B4ND3602
  domicile: IE
  product_type: ETC
  exchange: LSE
  source_url: https://www.ishares.com/uk/individual/en/products/258441/
  verified_on: 2026-06-13
# BTC + commodity entries: add ONLY after confirming ISIN + domicile against the
# issuer factsheet at build time (do not paste from memory). Leave absent until
# verified — absent = unverified = cannot hold, which is the correct safe default.
```

- [ ] **Step 4: Implement loader** (cache the parse; resolve path via `argosy` package root).

```python
from functools import lru_cache
from pathlib import Path
import yaml

@lru_cache(maxsize=1)
def load_registry() -> dict[str, dict]:
    p = Path(__file__).resolve().parent.parent / "data" / "verified_instruments.yaml"
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {k.upper(): v for k, v in data.items()}

def registry_lookup(symbol: str, registry: dict[str, dict]) -> dict | None:
    return registry.get(symbol.upper())
```

- [ ] **Step 5: Run to verify pass.** (If you cannot confirm SGLD's real ISIN at build time, mark the test xfail with a note rather than seeding an unverified ISIN — the doctrine forbids fabricated facts even in a fixture.)
- [ ] **Step 6: Commit** — `feat(alternatives): verified-instrument facts registry + loader`.

### Task C4: `verify_instrument` — compose checksum + coherence + registry

**Files:**
- Modify: `argosy/services/instrument_verification.py`
- Test: `tests/test_instrument_verification.py`

- [ ] **Step 1: Write the failing test**

```python
from argosy.services.instrument_verification import verify_instrument

def test_known_clean_instrument_verifies_green():
    r = verify_instrument(symbol="SGLD", claimed_domicile="IE", claimed_isin="IE00B579F325")
    assert r.verified and r.severity == "GREEN" and r.evidence.registry_hit

def test_us_prefix_isin_with_nonus_claim_is_red():
    # claims IE but the ISIN is a real US one → incoherent → never hold
    r = verify_instrument(symbol="SPY", claimed_domicile="IE", claimed_isin="US78462F1030")
    assert not r.verified and r.severity == "RED" and not r.evidence.domicile_coherent

def test_unknown_unverifiable_instrument_is_yellow_not_held():
    r = verify_instrument(symbol="MADEUP", claimed_domicile="IE", claimed_isin=None)
    assert not r.verified and r.severity == "YELLOW"
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement** (decision table: registry hit + checksum + coherence → GREEN; US prefix or failed checksum with non-US claim → RED; unknown/unstamped → YELLOW; nothing unverified is ever `verified=True`).

```python
from argosy.services.alternatives_types import VerificationEvidence, VerificationResult  # noqa

def verify_instrument(*, symbol: str, claimed_domicile: str | None,
                      claimed_isin: str | None) -> "VerificationResult":
    from argosy.services.alternatives_types import VerificationEvidence, VerificationResult
    reg = load_registry()
    hit = registry_lookup(symbol, reg)
    # Registry is authoritative when present: verify the CLAIM matches the cached fact.
    isin = (hit or {}).get("isin", claimed_isin)
    domicile = (hit or {}).get("domicile", claimed_domicile)
    checksum_ok = isin_is_valid(isin)
    prefix = isin_country_prefix(isin)
    # coherence: a US ISIN prefix contradicts any non-US domicile claim; otherwise
    # require the prefix to be a non-US country (XS is allowed for ETPs).
    coherent = bool(prefix) and not (prefix == "US" and (domicile or "").upper() != "US")
    src = (hit or {}).get("source_url")
    ev = VerificationEvidence(isin_checksum_ok=checksum_ok, isin_prefix=prefix,
                              domicile_coherent=coherent, registry_hit=hit is not None,
                              tradeable=None, source_url=src)
    if hit is not None and checksum_ok and coherent and (domicile or "").upper() != "US":
        return VerificationResult(symbol=symbol, verified=True, severity="GREEN",
                                  reason="registry-confirmed, checksum+coherence ok", evidence=ev)
    if (prefix == "US") or (isin and not checksum_ok):
        return VerificationResult(symbol=symbol, verified=False, severity="RED",
                                  reason="US-situs ISIN prefix or failed checksum", evidence=ev)
    return VerificationResult(symbol=symbol, verified=False, severity="YELLOW",
                              reason="unverified: not in registry / unstamped", evidence=ev)
```

- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Codex-tandem review the decision table** (`tmp_review/codex_verify_instrument_review.py`): "can a hallucinated instrument reach `verified=True`? find the hole." Apply blockers.
- [ ] **Step 6: Commit** — `feat(alternatives): verify_instrument decision table (no unverified holdings)`.

### Task C5: `verify_and_gate_proposal` (replace `gate_proposal`)

**Files:**
- Modify: `argosy/services/alternatives_sourcing.py`
- Test: `tests/test_alternatives_sourcer.py`

- [ ] **Step 1: Write the failing test** — feed an `AlternativesProposal` mixing one registry-clean pick and one hallucinated ISIN; assert only the verified one survives and the hallucinated one is a violation.

```python
from argosy.services.alternatives_sourcing import verify_and_gate_proposal
from argosy.agents.alternatives_sourcer import AlternativesProposal, AssetProposal

def test_only_verified_candidates_survive():
    prop = AlternativesProposal(
        sleeve_pct=3.0, rationale_md="x", cited_sources=["src"],
        proposals=[
            AssetProposal(symbol="SGLD", name="Invesco Physical Gold ETC",
                          asset_class="precious_metals", domicile="IE",
                          isin="IE00B579F325", weight_within_sleeve_pct=80,
                          conviction="HIGH", thesis_md="gold", cites=["f"]),
            AssetProposal(symbol="HALLUC", name="Fake ETP", asset_class="crypto",
                          domicile="JE", isin="JE00FAKE0000", weight_within_sleeve_pct=20,
                          conviction="LOW", thesis_md="nope", cites=[]),
        ],
    )
    clean, violations = verify_and_gate_proposal(prop)
    assert [c.symbol for c in clean] == ["SGLD"]
    assert any("HALLUC" in v for v in violations)
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement** `verify_and_gate_proposal`: for each proposal run `verify_instrument`; keep only `verified` AND estate-clean (re-run `validate_instrument_domicile` on the survivors as belt-and-suspenders); return `(list[VerifiedAlternativesCandidate], list[str] violations)`. Keep `gate_proposal` as a thin deprecated wrapper only if other callers exist (grep first; if none, delete it).

- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `feat(alternatives): verify_and_gate_proposal supersedes claim-only gate`.

---

## Phase D — Alternatives-aware fleet debate

### Task D1: alternatives reviewer agents

**Files:**
- Create: `argosy/agents/alternatives_reviewers.py`
- Modify: `argosy/agents/base.py` (register roles)
- Test: `tests/test_alternatives_reviewers.py`

Build three reviewer agents subclassing `BaseAgent`, each with a structured output model and an ETP-appropriate prompt (NO operating-company valuation — codex #4):
- `AltExposureStructureAnalyst` — wrapper/structure/cost/custody/replication review.
- `AltMacroDiversificationAnalyst` — diversification value vs the NVDA-heavy book; regime fit.
- `AltRiskLiquidityTaxAnalyst` — liquidity, spread, tracking, Israeli tax treatment, estate angle.

- [ ] **Step 1: Write failing tests** asserting each agent has its `agent_role`, `output_model`, and that `build_prompt` includes the verified candidates and excludes any "earnings/P/E/fair value" language. (Test the prompt string for absence of `"fair value"`, presence of `"wrapper"`/`"tracking"`.)
- [ ] **Step 2–4:** implement minimal agents + register roles; run tests green.
- [ ] **Step 5: Commit** — `feat(alternatives): ETP-aware reviewer fleet (no equity-valuation prompts)`.

### Task D2: alternatives fund-manager / synthesizer decision

**Files:**
- Modify: `argosy/agents/alternatives_reviewers.py`
- Test: `tests/test_alternatives_reviewers.py`

- [ ] Build `AlternativesFundManagerAgent` whose output is `AlternativesSleeveDecision`. Prompt: weigh verified candidates + reviewer reports; choose `target_pct` (0–4 cap, BTC ≤1% of book), set `decision ∈ {approve, cut, 0_percent, insufficient_data}`. `0_percent`/`insufficient_data` legitimate when no verified estate-clean candidates exist, evidence thin, or risk forces too much FI for too little benefit (codex #6).
- [ ] Test the three decision branches with stubbed reviewer inputs (monkeypatch `run_sync`). Assert `insufficient_data` ⇒ `target_pct == 0` and empty instruments.
- [ ] **Commit** — `feat(alternatives): sleeve FM decision (approve/cut/0%/insufficient_data)`.

### Task D3: debate-gate orchestration (the subflow)

**Files:**
- Create: `argosy/orchestrator/flows/plan_synthesis/alternatives_phase.py`
- Test: `tests/test_alternatives_phase.py`

- [ ] Implement `run_alternatives_phase(db, user_id, decision_run_id, macro_context) -> AlternativesSleeveDecision`: source → `verify_and_gate_proposal` → if zero verified candidates, short-circuit to a `0_percent` decision with provenance (do NOT proceed to debate) → else run the three reviewers + FM → assemble `AlternativesSleeveDecision`. **Hard gate first** (verified + estate-clean), then require ≥ source + verifier + 2 reviewer roles before any non-zero sleeve (codex #6). Persist `agent_reports` + a `decision_phases` row.
- [ ] Tests: (a) hallucinated-only proposal ⇒ `0_percent`; (b) clean proposal + approving reviewers ⇒ `approve` with instruments; (c) verification empty ⇒ never calls reviewers (assert via spy).
- [ ] **Commit** — `feat(alternatives): synthesis subflow source→verify→debate→decide`.

---

## Phase E — Engine + synthesis wiring; sigma; carry-forward

### Task E1: compute sleeve sigma from verified instruments

**Files:**
- Modify: `argosy/services/retirement/sigma_calibration.py`
- Test: `tests/test_sigma_calibration.py`

- [ ] **Step 1: Write failing test** — `compute_alternatives_sigma([(gold, 0.8), (btc, 0.2)]) == 0.268` (gold σ=0.16, BTC σ=0.70 linear blend per methodology verdict); empty ⇒ 0.0.

```python
def test_alternatives_sigma_linear_blend():
    from argosy.services.retirement.sigma_calibration import compute_alternatives_sigma
    assert round(compute_alternatives_sigma([("precious_metals", 0.8), ("crypto", 0.2)]), 3) == 0.268
    assert compute_alternatives_sigma([]) == 0.0
```

- [ ] **Step 2–4:** implement `compute_alternatives_sigma(weighted_classes)` mapping asset_class → per-class σ (`precious_metals=0.16`, `crypto=0.70`, `commodities=0.20`, default conservative `0.30`), weighted by within-sleeve weight; remove the fixed `_SIGMA_BY_CLASS["alternatives"]=0.268` reliance for sourced sleeves. Run green.
- [ ] **Step 5: Codex-tandem review** the per-class σ map + blend (`tmp_review/codex_alt_sigma_review.py`). Apply blockers.
- [ ] **Commit** — `feat(alternatives): sleeve sigma computed from verified instruments`.

### Task E2: engine consumes a supplied sleeve; remove the hardcode

**Files:**
- Modify: `argosy/services/allocation_plan.py`
- Test: `tests/test_allocation_plan.py`

- [ ] **Step 1: Write failing tests** — three fixtures:
  - verified 3% sleeve supplied ⇒ alternatives class present at 3%, FI rises to hold blended σ ≤ 0.18 anchor, equity sleeves fall pro-rata;
  - verified 0% / `None` sleeve ⇒ NO alternatives class, engine identical to pre-sleeve baseline;
  - supplied sleeve with sourced σ from E1 flows into the FI solver (not the fixed 0.268).

```python
def test_no_sleeve_means_no_alternatives_class():
    doc = build_target_allocation(..., alternatives_sleeve=None)
    assert not any(c.label.lower().startswith("alternatives") for c in doc.classes)

def test_supplied_sleeve_subtracts_before_renorm_and_holds_anchor():
    decision = AlternativesSleeveDecision(target_pct=3.0, sleeve_sigma=0.268, ...,
                                          decision="approve", rationale_md="x")
    doc = build_target_allocation(..., alternatives_sleeve=decision)
    alt = next(c for c in doc.classes if c.label.lower().startswith("alternatives"))
    assert round(alt.target_pct, 2) == 3.0
    assert doc.blended_sigma <= 0.18 + 1e-6
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** — add `alternatives_sleeve: AlternativesSleeveDecision | None = None` to `derive_fi_weight` / `_renormalise` / `build_target_allocation`; subtract `alternatives_sleeve.target_pct` before equity renorm; build the alternatives class from `alternatives_sleeve.instruments`; feed `alternatives_sleeve.sleeve_sigma` into the blended-sigma calc; **delete** `_ALTERNATIVES_SLEEVE`, `ALTERNATIVES_TARGET_PCT`/`_MAX_PCT`/`_GOLD_FRAC`/`_BTC_FRAC`, and the assert at line ~82. Keep the domicile Literal additions (`CH`, `JE`) and the sigma-class plumbing (mechanism, not picks).
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Codex-tandem review the renorm + anchor math** (`tmp_review/codex_engine_sleeve_review.py`): "does FI still solve the 0.18 anchor with an arbitrary supplied sleeve σ/size? off-by-one in the subtract-before-renorm?" Apply blockers.
- [ ] **Commit** — `feat(alternatives): engine consumes supplied sleeve; hardcode removed`.

### Task E3: no stale carry-forward of alternatives

**Files:**
- Modify: `argosy/services/target_allocation_doc.py`
- Test: `tests/test_target_allocation_doc.py`

- [ ] **Step 1: Write failing test** — when fresh verification yields no sleeve but a prior CURRENT doc had a 3% alternatives class, `resolve_target_allocation_json` must NOT carry forward the old alternatives class; it builds explicit `0%` (no alternatives class) with provenance noting verification failure. Non-alternatives classes may still carry forward.
- [ ] **Step 2–4:** implement: strip any alternatives class from a carried-forward doc and stamp provenance `"alternatives verification failed → 0% this run"`. Run green.
- [ ] **Step 5: Codex-tandem review** (`tmp_review/codex_carryforward_review.py`): "can a stale alternatives sleeve survive a failed fresh verification by any path?" Apply blockers.
- [ ] **Commit** — `fix(alternatives): never carry forward a stale sleeve past failed verification`.

### Task E4: thread the phase into `run_synthesis`

**Files:**
- Modify: `argosy/orchestrator/flows/plan_synthesis/orchestrator.py`
- Test: `tests/test_plan_synthesis_orchestrator.py` (or the existing synthesis test module)

- [ ] **Step 1: Write failing test** — `run_synthesis` calls `run_alternatives_phase` before `resolve_target_allocation_json`, and the resulting draft's `target_allocation_json` reflects the decision (monkeypatch the phase to return a known 3% decision; assert the draft doc has the 3% class). Assert the draft is `role="draft"`, never `"current"`.
- [ ] **Step 2–4:** wire it: call the phase, pass the `AlternativesSleeveDecision` into the allocation build path, persist reports. Run green.
- [ ] **Step 5: Commit** — `feat(alternatives): alternatives phase wired into plan synthesis (draft only)`.

---

## Phase F — Surface + verification

### Task F1: live end-to-end run (no promotion)

- [ ] Update `tmp_review/run_alternatives_sourcing.py` → a full-phase live run: source → verify → debate → decision; print the `AlternativesSleeveDecision` + every `VerificationResult`. Confirm a hallucinated ISIN is rejected and a clean one is GREEN. **Do NOT promote** (`role` stays `draft`).
- [ ] Confirm the existing `/plan` draft projection shows the alternatives class with its verification evidence (no new UI build needed if the draft doc already projects; otherwise note the gap — do not build UI without a go).

### Task F2: full regression + suite

- [ ] Touched-file suites green: `…pytest tests/test_instrument_verification.py tests/test_alternatives_types.py tests/test_alternatives_sourcer.py tests/test_alternatives_reviewers.py tests/test_alternatives_phase.py tests/test_allocation_plan.py tests/test_sigma_calibration.py tests/test_target_allocation_doc.py -q -p no:cacheprovider`.
- [ ] Full suite once at the end: `…pytest -m "not llm_eval" -p no:cacheprovider -q`. Expect ~1,020+ passing; reconcile any reds before claiming done.
- [ ] SDD handover note refreshed (current-state only, no history prose, per `feedback_docs_current_state_only`).

---

## Self-review (run against the spec — codex verdict's 10 steps)

1. Define `AlternativesSleeveDecision` (allow 0%) → **Task C1.** ✔
2. `InstrumentVerificationService` (registry + adapter for unknowns; ISIN checksum, symbol/exchange/issuer/domicile/type/tradeability) → **C2–C4** (checksum + coherence + registry; tradeability is the optional yfinance cross-check; symbol/exchange/issuer match folded into registry facts). ✔ *Gap: live issuer-page scrape for unknown unseeded candidates is deferred — unknowns are YELLOW/rejected, never held, which satisfies the safety contract; note this limitation in F1.*
3. LLM discovers/summarizes; deterministic verifier decides → **C4** (verifier is pure; agent only proposes). ✔
4. `verify_and_gate_proposal` replaces `gate_proposal` → **C5.** ✔
5. Alternatives-specific reviewers (no `TraderProposal`) → **D1–D2.** ✔
6. Debate gate (hard gates first; ≥ source+verifier+2 reviewers; 0% legitimate) → **D3 + D2.** ✔
7. `build_target_allocation(..., alternatives_sleeve=None)`, subtract-before-renorm, FI stays solver → **E2.** ✔
8. Dynamic sigma from verified instruments → **E1.** ✔
9. Thread through `run_synthesis` before `resolve_target_allocation_json`; draft only → **E4** (+ amendment workers: grep for other `resolve_target_allocation_json` callers in E4 and thread or document). ✔
10. Tests: verified-3% / verified-0% / hallucinated-ISIN-rejected + stale-carry-forward regression → **E2, C5, E3.** ✔

**Placeholder scan:** deterministic-core tasks carry full code; agent/prompt tasks (D1–D2, E4) specify exact roles, output types, and assertions but defer prose prompts to implementation (prompts can't be pre-written to the character — the test contracts pin their required/forbidden content). **Type consistency:** `AlternativesSleeveDecision`, `VerifiedAlternativesCandidate`, `VerificationResult`, `VerificationEvidence` used identically across C1/C5/D2/E2. **Known deferral:** live scrape for un-seeded unknowns (step-2 "adapter for unknowns") — safe because unverified ⇒ never held; flagged for a follow-up if the team routinely proposes off-registry instruments.
