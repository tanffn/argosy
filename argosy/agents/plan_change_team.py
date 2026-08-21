"""Plan-change team — the fleet AUTHORS plan-content changes, verified by blind re-derivation.

Two judgment decisions for the living plan, each made by a REAL agent and
verified by ANOTHER agent that re-derives independently from the same RAW
facts (never seeing the author's reasoning) — divergence is compared IN CODE
and surfaced, never auto-resolved (feedback_adversarial_review_must_re_derive_blind):

  1. **Sleeve-instrument selection** (e.g. replace R1GR — the ~14%-NVDA
     Russell 1000 Growth UCITS — as the US-growth sleeve primary): the author
     picks ONE instrument from a sourced candidate table; a blind reviewer
     re-derives its own pick from the same table.
  2. **Diversifier-sleeve adjudication** (gold ON TRIAL vs growth-bearing
     diversifiers): burden of proof on gold; criterion is the PRIME DIRECTIVE
     (earliest safe retirement on the household's ACTUAL book), not
     volatility-damping for its own sake.

Determinism stays out of the judgment: the arithmetic floor (targets sum,
single-name cap, estate/domicile) is enforced downstream by the plan risk
kernel + domicile guardrail on the staged draft.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from argosy.agents._plan_authority import PRIME_DIRECTIVE
from argosy.agents.base import BaseAgent
from argosy.services.allocation_plan import NVDA_TARGET_PCT as _NVDA_TARGET_PCT
from argosy.services.retirement.scenario_mc import DEFAULT_NVDA_CAP_PCT as _DEFAULT_NVDA_CAP_PCT


# --------------------------------------------------------------------------
# Output schemas (flat — the bundled claude.exe chokes on nested $defs, so we
# take the prose-JSON path like DeploymentAuthorAgent).
# --------------------------------------------------------------------------
class InstrumentSwapDecision(BaseModel):
    chosen_symbol: str
    chosen_name: str = ""
    isin: str = ""
    domicile: str = ""
    nvda_weight_pct: float = 0.0
    us_weight_pct: float = 0.0
    rationale: str
    runner_up_symbol: str = ""
    runner_up_reason: str = ""


class DiversifierAdjudication(BaseModel):
    gold_wins: bool
    chosen_symbol: str
    chosen_name: str = ""
    sleeve_pct: float = Field(ge=0.0, le=10.0)
    gold_verdict_md: str
    rationale: str
    funding_note: str = ""


def _facts_block(candidates: list[dict[str, Any]]) -> str:
    lines = []
    for c in candidates:
        parts = [f"{c.get('symbol')}: {c.get('name', '')}"]
        for k in ("isin", "domicile", "ter", "aum", "index", "nvda_weight_pct",
                  "us_weight_pct", "yield_pct", "character", "notes"):
            if c.get(k) not in (None, ""):
                parts.append(f"{k}={c[k]}")
        lines.append("  - " + "; ".join(str(p) for p in parts))
    return "\n".join(lines) or "  (none)"


def _book_block(book: dict[str, Any]) -> str:
    holdings = book.get("holdings") or {}
    hl = "\n".join(
        f"    - {s}: ${v:,.0f}" for s, v in sorted(holdings.items(), key=lambda kv: -kv[1])
    )
    return (
        f"  - tradeable book: ${book.get('book_usd', 0):,.0f}\n"
        f"  - NVDA look-through TODAY: {book.get('nvda_lookthrough_pct', 0):.1f}% "
        f"(transition; plan glide sells it down toward the "
        f"{_NVDA_TARGET_PCT:.0f}% direct target / "
        f"{_DEFAULT_NVDA_CAP_PCT * 100:.0f}% cap)\n"
        f"  - US-facing look-through TODAY: {book.get('us_facing_pct', 0):.1f}%\n"
        f"  - household income: NVIDIA salary (same complex as the equity concentration)\n"
        f"  - HOLDINGS (USD):\n{hl}"
    )


def _plan_block(plan_targets: dict[str, float]) -> str:
    return "\n".join(
        f"  - {label}: {pct}%" for label, pct in plan_targets.items()
    ) or "  (none)"


class SleeveInstrumentAuthorAgent(BaseAgent[InstrumentSwapDecision]):
    """Authors ONE sleeve-primary instrument pick from a sourced candidate table."""

    agent_role = "plan_instrument_author"
    output_model = InstrumentSwapDecision
    require_citations = False
    use_structured_output = False
    claude_code_max_retries = 1

    def build_prompt(
        self,
        *,
        sleeve_mandate: str,
        constraints: str,
        candidates: list[dict[str, Any]],
        book: dict[str, Any],
        plan_targets: dict[str, float],
    ) -> tuple[str, str]:
        system = (
            "You are the plan-instrument author on the Argosy fleet, choosing the "
            "PRIMARY instrument for one sleeve of a long-hold, Israeli-resident "
            "(non-US-person) investor's strategic plan.\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            "Choose from the SOURCED CANDIDATE FACTS only — never invent an "
            "instrument or trust a label over the sourced weights. Reason from the "
            "book's ACTUAL exposures (look-through, not fund names). Record an "
            "honest rationale: what the pick gives up as well as what it fixes.\n\n"
            "OUTPUT: a single JSON object with keys chosen_symbol, chosen_name, "
            "isin, domicile, nvda_weight_pct, us_weight_pct, rationale, "
            "runner_up_symbol, runner_up_reason. No prose outside the JSON."
        )
        user = (
            f"SLEEVE MANDATE:\n{sleeve_mandate}\n\n"
            f"HARD CONSTRAINTS:\n{constraints}\n\n"
            f"THE BOOK TODAY (raw):\n{_book_block(book)}\n\n"
            f"PLAN TARGET SLEEVES (current plan v64):\n{_plan_block(plan_targets)}\n\n"
            f"SOURCED CANDIDATE FACTS (cited from issuer/justETF factsheets):\n"
            f"{_facts_block(candidates)}\n\n"
            "Author the pick now."
        )
        return system, user


class SleeveInstrumentBlindReviewerAgent(BaseAgent[InstrumentSwapDecision]):
    """BLIND re-derivation of the same instrument choice — sees the same raw
    facts, NEVER the author's pick or reasoning. Code compares the two picks;
    divergence is surfaced, not auto-resolved."""

    agent_role = "plan_instrument_blind_reviewer"
    output_model = InstrumentSwapDecision
    require_citations = False
    use_structured_output = False
    claude_code_max_retries = 1

    def build_prompt(
        self,
        *,
        sleeve_mandate: str,
        constraints: str,
        candidates: list[dict[str, Any]],
        book: dict[str, Any],
        plan_targets: dict[str, float],
    ) -> tuple[str, str]:
        system = (
            "You are an independent reviewer on the Argosy fleet. ANOTHER agent has "
            "already chosen a primary instrument for the sleeve below — you have NOT "
            "seen its choice or reasoning, and you must not try to guess it. Your job "
            "is to RE-DERIVE the best pick yourself, from the raw sourced facts alone, "
            "as a check against the author. Be adversarial with every candidate: "
            "verify each one actually satisfies the hard constraints from its sourced "
            "numbers before considering it.\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            "OUTPUT: a single JSON object with keys chosen_symbol, chosen_name, isin, "
            "domicile, nvda_weight_pct, us_weight_pct, rationale, runner_up_symbol, "
            "runner_up_reason. No prose outside the JSON."
        )
        user = (
            f"SLEEVE MANDATE:\n{sleeve_mandate}\n\n"
            f"HARD CONSTRAINTS:\n{constraints}\n\n"
            f"THE BOOK TODAY (raw):\n{_book_block(book)}\n\n"
            f"PLAN TARGET SLEEVES (current plan v64):\n{_plan_block(plan_targets)}\n\n"
            f"SOURCED CANDIDATE FACTS (cited from issuer/justETF factsheets):\n"
            f"{_facts_block(candidates)}\n\n"
            "Derive your own pick now."
        )
        return system, user


_DIVERSIFIER_SYSTEM = (
    "You are adjudicating the DIVERSIFIER SLEEVE for a long-hold, Israeli-resident "
    "(non-US-person) investor whose book is heavily concentrated in the NVDA/US-tech/"
    "USD complex AND whose salary is NVIDIA. The plan currently holds 0% in anything "
    "uncorrelated with that complex.\n\n"
    "THE QUESTION ON TRIAL: should the new ~3-5% sleeve be a small PHYSICAL-GOLD "
    "slice, or a GROWTH-BEARING diversifier? The client rejected gold as a default "
    "('investing in a metal is lame') but added: 'I am not the expert — if gold is "
    "the right move I will add it.' So adjudicate ON THE MERITS, with the burden of "
    "proof ON GOLD: gold must beat the best growth-bearing alternative, not merely "
    "diversify.\n\n"
    f"{PRIME_DIRECTIVE}\n\n"
    "DECISION CRITERION: earliest SAFE retirement — the total-portfolio outcome on "
    "this household's ACTUAL book (concentration + NVIDIA employment income), NOT "
    "volatility-damping for its own sake. A diversifier that damps volatility but "
    "drags expected return can DELAY retirement; a 'diversifier' that re-buys the "
    "US/tech complex diversifies nothing. Weigh both failure modes.\n"
    "  - If GOLD wins, the rationale MUST explicitly answer the 'gold produces "
    "nothing' objection (why a non-yielding metal still buys an earlier safe "
    "retirement for THIS book).\n"
    "  - If gold LOSES, say so explicitly and pick the growth-bearing diversifier.\n"
    "  - Also note (funding_note) which over-tilted US sleeves should be trimmed "
    "to fund the sleeve, in ~whole percentage points.\n\n"
    "Constraints: instruments must be Irish/Lux UCITS or an Irish ETC (estate-gated "
    "core; non-US-situs for a non-US-person). One primary instrument for the sleeve. "
    "Sleeve size 3-5% — you decide the exact number.\n\n"
    "OUTPUT: a single JSON object with keys gold_wins (bool), chosen_symbol, "
    "chosen_name, sleeve_pct (number), gold_verdict_md, rationale, funding_note. "
    "No prose outside the JSON."
)


class DiversifierAdjudicatorAgent(BaseAgent[DiversifierAdjudication]):
    """Adjudicates gold-vs-growth-diversifier on the merits for the household book."""

    agent_role = "plan_diversifier_adjudicator"
    output_model = DiversifierAdjudication
    require_citations = False
    use_structured_output = False
    claude_code_max_retries = 1

    def build_prompt(
        self,
        *,
        candidates: list[dict[str, Any]],
        evidence_md: str,
        book: dict[str, Any],
        plan_targets: dict[str, float],
        blind_rederive: bool = False,
    ) -> tuple[str, str]:
        system = _DIVERSIFIER_SYSTEM
        if blind_rederive:
            system = (
                "You are an INDEPENDENT reviewer: another agent has already "
                "adjudicated this question — you have NOT seen its verdict and must "
                "not guess it; re-derive your own from the raw facts alone (your "
                "verdict is compared in code and divergence is surfaced).\n\n"
            ) + system
        user = (
            f"THE BOOK TODAY (raw):\n{_book_block(book)}\n\n"
            f"PLAN TARGET SLEEVES (current plan v64):\n{_plan_block(plan_targets)}\n\n"
            f"SOURCED CANDIDATE FACTS (cited):\n{_facts_block(candidates)}\n\n"
            f"SOURCED EVIDENCE (both sides of the gold debate):\n{evidence_md}\n\n"
            "Adjudicate now."
        )
        return system, user


# --------------------------------------------------------------------------
# x10 moonshot sleeve composition — author + blind re-derivation.
#
# The permanent ~5% high-growth sleeve is re-sourced under the BINDING x10
# asymmetry mandate (high_potential_sleeve.X10_SLEEVE_MANDATE): cap-math test,
# accepted per-name loss = 100% (defensibility never boosts rank), rank =
# (upside x plausibility) / DOWNSIDE (a countable floor RAISES rank — mandate c/c2),
# weights = the deploy fill order. The author
# re-grades the current names and may source new candidates (WebSearch); the
# blind reviewer re-derives independently; divergence is compared IN CODE and
# forces a reconciliation round — never auto-resolved.
# --------------------------------------------------------------------------
class MoonshotName(BaseModel):
    ticker: str
    action: str = "KEEP"          # KEEP | ADD | EXIT
    weight_pct: float = Field(default=0.0, ge=0.0, le=100.0)  # of the sleeve; 0 for EXIT
    cap_math: str = ""            # one line: cap today -> plausible outcome -> multiple
    downside_math: str = ""       # one line: the FLOOR — price/book, net cash vs
                                  # cap, or revenue at a defensible multiple; or an
                                  # explicit "no floor". Mandate (c2): undeclared
                                  # == none, and unfloored never outranks floored
                                  # at equal upside. Without this the rank is
                                  # variance, not asymmetry.
    disposition: str = ""         # for EXIT: what to do with any existing fill


class MoonshotSleeveComposition(BaseModel):
    names: list[MoonshotName] = Field(default_factory=list)
    rationale: str = ""


_MOONSHOT_OUTPUT_SPEC = (
    "OUTPUT: a single JSON object {\"names\": [{\"ticker\": str, \"action\": "
    "\"KEEP|ADD|EXIT\", \"weight_pct\": number, \"cap_math\": str, "
    "\"downside_math\": str, \"disposition\": str}], \"rationale\": str}. "
    "Rules: KEEP/ADD weights are % "
    "of the sleeve, MUST sum to 100, and MUST be ordered + sized "
    "asymmetry-first (highest asymmetry rank = largest weight = filled first); "
    "EXIT names get weight_pct=0 plus a disposition (existing small fills are "
    "held or migrated on a scheduled rebalance — never a forced sell); every "
    "name's cap_math is ONE line with real numbers: market cap today -> "
    "plausible 5-10y outcome -> implied multiple; and every name's "
    "downside_math is ONE line with real numbers naming the FLOOR "
    "(price/book, net cash vs market cap, or revenue at a defensible "
    "multiple) or the literal words 'no floor'. A name whose downside_math "
    "is blank is scored as HAVING NO FLOOR and must rank below any floored "
    "name of equal upside. 6-10 surviving names. No prose outside the JSON."
)


def _moonshot_user_block(
    current_sleeve: list[dict[str, Any]],
    book: dict[str, Any],
    notes: str,
) -> str:
    cur = "\n".join(
        f"  - {c.get('ticker')}: weight {c.get('weight_pct')}% — "
        f"{(c.get('thesis') or '')[:200]}"
        for c in current_sleeve
    ) or "  (none)"
    return (
        f"CURRENT SLEEVE COMPOSITION (plan v66, ~5% of the book):\n{cur}\n\n"
        f"THE BOOK TODAY (raw):\n{_book_block(book)}\n\n"
        f"CONTEXT NOTES:\n{notes or '  (none)'}\n\n"
        "Use WebSearch to verify each name's CURRENT market cap and to source "
        "any new candidates; apply the cap-math test to every name with real "
        "numbers. Compose the sleeve now."
    )


class MoonshotSleeveAuthorAgent(BaseAgent[MoonshotSleeveComposition]):
    """Authors the x10 sleeve composition under the binding asymmetry mandate."""

    agent_role = "moonshot_sleeve_author"   # not in tables -> Opus fallback
    output_model = MoonshotSleeveComposition
    require_citations = False
    use_structured_output = False
    claude_code_max_retries = 1
    claude_code_allowed_tools = ("WebSearch", "WebFetch")

    def build_prompt(
        self,
        *,
        current_sleeve: list[dict[str, Any]],
        book: dict[str, Any],
        notes: str = "",
    ) -> tuple[str, str]:
        from argosy.services.high_potential_sleeve import X10_SLEEVE_MANDATE

        system = (
            "You are the moonshot-sleeve author on the Argosy fleet, composing "
            "the permanent ~5% high-growth sleeve of a long-hold, Israeli-"
            "resident (non-US-person) investor's strategic plan. The sleeve is "
            "deliberately domicile-agnostic (not estate-gated).\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            f"{X10_SLEEVE_MANDATE}\n\n"
            "Re-grade EVERY current name against the mandate (verify its market "
            "cap live — do not trust memory) and EXIT any that fail the "
            "cap-math test, however good the company: index-covered maybe-2x "
            "compounders are the opposite of this sleeve's job. You may ADD new "
            "candidates you source yourself, applying the same test.\n\n"
            f"{_MOONSHOT_OUTPUT_SPEC}"
        )
        return system, _moonshot_user_block(current_sleeve, book, notes)


class MoonshotSleeveBlindReviewerAgent(BaseAgent[MoonshotSleeveComposition]):
    """BLIND re-derivation of the sleeve composition — same raw inputs, never
    the author's picks or reasoning. Code compares the two compositions;
    divergence forces reconciliation, never auto-resolution."""

    agent_role = "moonshot_sleeve_blind_reviewer"
    output_model = MoonshotSleeveComposition
    require_citations = False
    use_structured_output = False
    claude_code_max_retries = 1
    claude_code_allowed_tools = ("WebSearch", "WebFetch")

    def build_prompt(
        self,
        *,
        current_sleeve: list[dict[str, Any]],
        book: dict[str, Any],
        notes: str = "",
    ) -> tuple[str, str]:
        from argosy.services.high_potential_sleeve import X10_SLEEVE_MANDATE

        system = (
            "You are an independent reviewer on the Argosy fleet. ANOTHER agent "
            "has already composed the moonshot sleeve below — you have NOT seen "
            "its composition or reasoning, and you must not try to guess it. "
            "RE-DERIVE the sleeve yourself from the raw inputs and the binding "
            "mandate, as an adversarial check: verify every market cap live and "
            "be ruthless with the cap-math test.\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            f"{X10_SLEEVE_MANDATE}\n\n"
            f"{_MOONSHOT_OUTPUT_SPEC}"
        )
        return system, _moonshot_user_block(current_sleeve, book, notes)


SLEEVE_CLASSES = ("ASSET_BACKED", "EARNING_POWER", "FUNDED_OPTIONALITY")

# Mandate (c2): an UNCLASSIFIED name is scored as FUNDED_OPTIONALITY -- the
# smallest cut -- so vagueness can never buy a larger allocation. The legacy
# FLOORED/UNFLOORED labels also land here on purpose: "floored" was the
# discredited concept (agents reported operating viability as downside
# protection), so a name still carrying it must be re-authored under the
# three-class model rather than inheriting its old weight.
def _floor_class(name) -> str:
    up = (getattr(name, "downside_math", "") or "").upper()
    for c in ("ASSET_BACKED", "EARNING_POWER"):
        if c in up:
            return c
    return "FUNDED_OPTIONALITY"
    if "FLOORED" in up:
        return "FLOORED"
    return "UNFLOORED"


def moonshot_divergences(
    author: MoonshotSleeveComposition,
    reviewer: MoonshotSleeveComposition,
    *,
    weight_tolerance_pp: float = 5.0,
) -> list[str]:
    """Deterministic comparison of the two blind compositions. Returns a list of
    human-readable divergences (empty = agreement). Compared IN CODE -- the
    reviewer never adjudicates its own agreement.

    Compares three things: INCLUSION, WEIGHT, and -- added 2026-08-21 -- the
    FLOOR CLASSIFICATION. The first two alone would have MISSED the failure that
    motivated this: on 2026-08-21 the author called RXRX floored on "real revenue"
    ($55M at 34x sales, negative gross profit) and OKLO unfloored despite the
    largest cash cushion of the four. That is a mis-LABEL, not a mis-weight, and
    it is exactly the class of factual claim two independent derivations should be
    forced to reconcile.
    """
    def _kept(c: MoonshotSleeveComposition) -> dict[str, float]:
        return {
            n.ticker.upper(): float(n.weight_pct)
            for n in c.names
            if n.action.upper() in ("KEEP", "ADD") and n.weight_pct > 0
        }

    def _floors(c: MoonshotSleeveComposition) -> dict[str, str]:
        return {
            n.ticker.upper(): _floor_class(n)
            for n in c.names
            if n.action.upper() in ("KEEP", "ADD") and n.weight_pct > 0
        }

    a, r = _kept(author), _kept(reviewer)
    af, rf = _floors(author), _floors(reviewer)
    out: list[str] = []
    for t in sorted(a.keys() - r.keys()):
        out.append(f"{t}: author keeps at {a[t]:.0f}%, reviewer excludes it")
    for t in sorted(r.keys() - a.keys()):
        out.append(f"{t}: reviewer keeps at {r[t]:.0f}%, author excludes it")
    for t in sorted(a.keys() & r.keys()):
        if abs(a[t] - r[t]) > weight_tolerance_pp:
            out.append(
                f"{t}: weight diverges by {abs(a[t] - r[t]):.0f}pp "
                f"(author {a[t]:.0f}% vs reviewer {r[t]:.0f}%)"
            )
        if af.get(t) != rf.get(t):
            out.append(
                f"{t}: SLEEVE CLASS diverges - author says {af.get(t)}, "
                f"reviewer says {rf.get(t)}. One of them has misread the balance "
                "sheet or the class definitions; reconcile against the filings "
                "before this name is sized."
            )
    return out


# --------------------------------------------------------------------------
# NVDA glide-SCHEDULE adjudication — author + blind re-derivation.
#
# The question on trial: over how many months / which per-tax-year quotas
# should the plan's NVDA deconcentration glide run (12mo / 24mo / a
# tax-year-optimized split)? The 12-month glide in plan v67 was inherited,
# not deliberately adjudicated — this pair makes the decision deliberate.
# Both agents get the SAME deterministic facts pack (position, basis,
# per-schedule CGT arithmetic, exposure-months, the deconcentration-optimizer
# FI grid); the blind reviewer never sees the author's verdict. Divergence is
# compared IN CODE and forces a reconciliation round — never auto-resolved.
# The verdict reaches the owner as a needs-confirm inbox proposal; it is
# NEVER auto-applied to the plan (a schedule change = governed re-synthesis).
# --------------------------------------------------------------------------
class GlideScheduleVerdict(BaseModel):
    chosen_schedule: str          # "12mo" | "24mo" | "tax_year_optimized" | short custom label
    horizon_months: int = Field(ge=1, le=60)
    quota_2026_shares: float = Field(ge=0.0)
    quota_2027_shares: float = Field(ge=0.0)
    quota_2028_shares: float = Field(ge=0.0)
    rationale: str
    tradeoff_sentence: str        # ONE sentence: months of concentration risk vs tax delta
    changes_current_glide: bool


_GLIDE_SCHEDULE_OUTPUT_SPEC = (
    "OUTPUT: a single JSON object with keys chosen_schedule (string label), "
    "horizon_months (int), quota_2026_shares, quota_2027_shares, "
    "quota_2028_shares (numbers — NVDA shares to sell per Israeli calendar "
    "tax year; 0 for years with no sales), rationale, tradeoff_sentence "
    "(ONE sentence stating months of concentration exposure vs the tax "
    "delta in NIS), changes_current_glide (bool — true iff your schedule "
    "differs materially from the current 12-month glide). No prose outside "
    "the JSON."
)

_GLIDE_SCHEDULE_SYSTEM = (
    "You are adjudicating the NVDA deconcentration SCHEDULE for a long-hold, "
    "Israeli-resident (non-US-person) investor whose salary is also NVIDIA. "
    "The strategic plan already decided the DESTINATION (NVDA down to its "
    "single-stock sleeve target; that is NOT on trial). ON TRIAL is only the "
    "PACE: should the glide run ~12 months (the current, inherited schedule), "
    "~24 months, a tax-year-optimized quota schedule, or something else you "
    "derive — expressed as Dec-31 Israeli-calendar-tax-year share quotas, "
    "which is how the owner actually manages sales.\n\n"
    f"{PRIME_DIRECTIVE}\n\n"
    "DECISION CRITERION: earliest SAFE retirement on the household's ACTUAL "
    "book. Weigh BOTH failure modes with the numbers in the facts pack:\n"
    "  - Slower schedules keep a very large single-name + employer-correlated "
    "exposure on the book longer (sequence risk; the facts pack quantifies "
    "exposure-months above 30%/20% weight and the optimizer's FI-age grid).\n"
    "  - Faster schedules bunch the realized real gain into fewer tax years "
    "(Israeli CGT surtax arithmetic is in the facts pack — note carefully "
    "WHICH surtax layers are actually avoidable by spreading, given salary "
    "already exceeds the threshold) and give up option value / deferral.\n\n"
    "HARD CONSTRAINTS: never schedule sales beyond the Section-102 "
    "capital-track-eligible pool at the capital rate (selling ineligible "
    "shares early is ordinary income at ~50-62% — a different, worse trade); "
    "quotas must sum to approximately the shares-to-target in the facts "
    "pack. Ground EVERY number you use in the facts pack — do not invent "
    "rates, thresholds, or share counts.\n\n"
    f"{_GLIDE_SCHEDULE_OUTPUT_SPEC}"
)


class GlideScheduleAdjudicatorAgent(BaseAgent[GlideScheduleVerdict]):
    """Adjudicates the NVDA glide pace (12mo/24mo/tax-optimized) on the merits."""

    agent_role = "plan_glide_schedule_adjudicator"   # not in tables -> Opus fallback
    output_model = GlideScheduleVerdict
    require_citations = False
    use_structured_output = False
    claude_code_max_retries = 1

    def build_prompt(
        self,
        *,
        facts_md: str,
        blind_rederive: bool = False,
        reconcile_md: str = "",
    ) -> tuple[str, str]:
        system = _GLIDE_SCHEDULE_SYSTEM
        if blind_rederive:
            system = (
                "You are an INDEPENDENT reviewer: another agent has already "
                "adjudicated this question — you have NOT seen its verdict and "
                "must not guess it; re-derive your own schedule from the raw "
                "facts alone (your verdict is compared in code and divergence "
                "forces a reconciliation round).\n\n"
            ) + system
        user = (
            "DETERMINISTIC FACTS PACK (raw; every number cites its source):\n\n"
            f"{facts_md}\n\n"
        )
        if reconcile_md:
            user += (
                "RECONCILIATION ROUND (code-forced): an independent blind "
                "reviewer re-derived the schedule from the same raw facts and "
                "reached a DIFFERENT verdict. Its re-derivation is below. "
                "Reconcile ON THE NUMBERS: either concede to the reviewer's "
                "schedule (and say what you got wrong) or refute it with "
                "specific arithmetic from the facts pack. Output your FINAL "
                "verdict in the same JSON schema.\n\n"
                f"--- BLIND REVIEWER'S RE-DERIVATION ---\n{reconcile_md}\n\n"
            )
        user += "Adjudicate the schedule now."
        return system, user


def glide_schedule_divergences(
    author: GlideScheduleVerdict,
    reviewer: GlideScheduleVerdict,
    *,
    month_tolerance: int = 3,
    share_tolerance: float = 500.0,
) -> list[str]:
    """Deterministic comparison of two blind schedule verdicts. Returns
    human-readable divergences (empty = agreement). Compared IN CODE — the
    reviewer never adjudicates its own agreement."""
    out: list[str] = []
    if abs(int(author.horizon_months) - int(reviewer.horizon_months)) > month_tolerance:
        out.append(
            f"horizon diverges: author {author.horizon_months}mo vs "
            f"reviewer {reviewer.horizon_months}mo"
        )
    for year in (2026, 2027, 2028):
        a = float(getattr(author, f"quota_{year}_shares"))
        r = float(getattr(reviewer, f"quota_{year}_shares"))
        if abs(a - r) > share_tolerance:
            out.append(
                f"tax-year {year} quota diverges by {abs(a - r):,.0f} shares "
                f"(author {a:,.0f} vs reviewer {r:,.0f})"
            )
    if author.changes_current_glide != reviewer.changes_current_glide:
        out.append(
            f"keep-vs-change diverges: author changes_current_glide="
            f"{author.changes_current_glide}, reviewer={reviewer.changes_current_glide}"
        )
    return out


__all__ = [
    "InstrumentSwapDecision",
    "DiversifierAdjudication",
    "SleeveInstrumentAuthorAgent",
    "SleeveInstrumentBlindReviewerAgent",
    "DiversifierAdjudicatorAgent",
    "MoonshotName",
    "MoonshotSleeveComposition",
    "MoonshotSleeveAuthorAgent",
    "MoonshotSleeveBlindReviewerAgent",
    "moonshot_divergences",
    "GlideScheduleVerdict",
    "GlideScheduleAdjudicatorAgent",
    "glide_schedule_divergences",
]
