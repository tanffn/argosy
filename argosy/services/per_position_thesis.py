"""Per-position thesis derivation (T4.1).

Pure-Python transformer — no LLM, no new agent class. Reads:

  * The pending draft's horizon JSONs (``horizon_short_json`` /
    ``horizon_medium_json`` / ``horizon_long_json``) to learn which
    tickers the plan wants to grow, trim, or reshape.
  * The current portfolio snapshot (positions + USD values) so we can
    compute "current weight" + "delta from target weight".
  * The synthesis run's ``agent_reports`` rows (response_text +
    sources_json) so we can attribute conviction + cited sources to
    each ticker.

Emits a list of :class:`PositionThesis` cards:

  * One per held ticker with a verdict ``HOLD|BUY|TRIM|SELL`` derived
    from horizon targets/actions.
  * One per "should add" ticker (verdict ``ADD``) — tickers the plan
    mentions in its action labels/details but which aren't in the user's
    portfolio today (UCITS replacements like ``XEON`` / ``ERNA`` /
    ``CSPX`` show up here).

The plan refers to this module as ``argosy/agents/per_position_thesis.py``
but it's not an agent — no LLM is invoked. Living under
``argosy/services/`` matches the project convention for pure derivation
helpers (cf. ``portfolio_snapshot_store.py``, ``agent_tree_builder.py``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Tunables — kept module-level so the test suite can verify the exact
# thresholds via inspection rather than mocking. The values are tuned
# against the May 2026 NVDA/SCHD/SGOV draft and documented in the T4.1
# return summary so future revisions can argue from data.
# NOTE (2026-08-21): the old "large reduction ⇒ SELL" ratio heuristic
# (_TRIM_VS_SELL_RATIO) is REMOVED per the conviction-model rewrite. SELL now
# means target weight zero; any positive target, however large the cut, is
# TRIM (see ``_classify_position``, band ``TARGET_ZERO`` vs ``ABOVE_MAX``).

_REASONING_CAP_CHARS = 500
"""Hard cap on the reasoning_md field per card. Plenty of room for 2-3
sentences; UI doesn't need a wall of text."""


# US-domiciled ETFs the domicile-aware canonical plan replaces with their Irish
# UCITS twin (see allocation_plan.py / feedback_canonical_allocation_ucits_preferred).
# A held position in any of these reads as TRIM/SELL only because the plan target
# is the UCITS twin — NOT a momentum or fundamental call. The card prepends an
# estate-tax domicile note so a long-hold investor isn't told to "sell SCHD"
# without the real reason.
_US_DOMICILED_UCITS_SWAP: dict[str, str] = {
    "VOO": "CSPX", "VTI": "CSPX",
    "SCHD": "FUSA", "VIG": "FUSA",
    "VEA": "EXUS", "VXUS": "EXUS",
    "SCHG": "R1GR",
    "USMV": "SPMV",
    "VNQ": "DPYA",
    "SGOV": "IB01",
    "VGSH": "IBTA",
}


# ---------------------------------------------------------------------------
# Constraint-vs-forecast conviction model (2026-08-21, Ariel-directed).
#
# The fleet was confusing uncertainty about ALPHA (forecast_confidence) with
# uncertainty about ACTION (action_conviction — the ``conviction`` field).
# Stock forecasting deserves low conviction; policy compliance, domicile/situs
# and duplicate-exposure decisions do not. See the ownership brief for the
# full taxonomy; this block implements it.
#
#   action_conviction = min(rule_confidence, confidence of every NECESSARY
#                            decision input, conflict-resolution confidence,
#                            tax/execution confidence when realization is
#                            required)
#
# Only inputs marked necessary=True impose the floor. A missing fair value
# caps a VALUATION-driven (FORECAST) action at LOW; it must NOT cap a
# policy-cap or domicile decision (those never touch forecast_confidence).
# ---------------------------------------------------------------------------

# Fallback policy constants — mirror argosy.services.decision_funnel.policy
# (RoutingPolicy.fallback_nvda_cap_pct / fallback_general_single_name_cap_pct)
# and argosy.services.ips (GENERAL_SINGLE_NAME_CAP_PCT). Used when no live IPS
# is available (unit tests, or a session-less caller) — deterministic and
# documented, never a silent guess.
_FALLBACK_NVDA_CAP_PCT = 13.0
_FALLBACK_GENERAL_SINGLE_NAME_CAP_PCT = 10.0
# Mirrors argosy.services.ips.SANCTIONED_US_SITUS — the one held US-situs name
# permitted despite the domicile tail (concentrated RSU stock).
_SANCTIONED_US_SITUS: frozenset[str] = frozenset({"NVDA"})

# Mega-cap US single names whose economic exposure is already fully contained
# in any broad-market core index the book holds — a duplicate, not a distinct
# thesis. Deliberately NARROW (vs. every US-situs stock): a small speculative
# satellite name (SOFI, RKLB, ...) is a PERMITTED holding whose slot must be
# earned by its own thesis, not an auto-duplicate. This is a documented proxy
# for true look-through decomposition (out of scope for a pure derivation
# layer) — see argosy.services.exposure_attribution for the sleeve-level
# (fund-vs-fund) version of this idea, which doesn't cover single-name-vs-index.
_MEGA_CAP_DUPLICATE_CANDIDATES: frozenset[str] = frozenset({
    "GOOG", "GOOGL", "META", "AMZN", "MSFT", "AAPL",
})
# Any held ticker in this set is read as "the book already has a core
# broad-market index sleeve" for duplication purposes.
_CORE_INDEX_HOLDINGS: frozenset[str] = frozenset({
    "CSPX", "VOO", "VTI", "FUSA", "R1GR", "SCHG", "QQQM",
})

# Sizing-band drift thresholds (relative to current weight) — same numbers the
# old single-path classifier used, now scoped to the "explicit plan target
# known" branch only.
_ADD_TRIGGER_DRIFT_REL = 0.10
_TRIM_TRIGGER_DRIFT_REL = -0.05


def _conviction_rank(value: str | None) -> int:
    return {"LOW": 0, "MEDIUM": 1, "MED": 1, "HIGH": 2}.get((value or "").upper(), 0)


def _min_conviction(*values: str | None) -> str:
    """min(...) over the HIGH>MEDIUM>LOW confidence lattice, ignoring None."""
    present = [v for v in values if v]
    if not present:
        return "LOW"
    worst = min(present, key=_conviction_rank)
    return "MEDIUM" if worst.upper() == "MED" else worst.upper()


def _di(
    name: str,
    value: Any,
    source: str,
    *,
    confidence: str,
    necessary: bool = True,
    freshness: str | None = None,
    quality: str | None = None,
) -> dict[str, Any]:
    """One ``decision_inputs`` entry — the audit trail behind the
    action_conviction floor."""
    return {
        "name": name,
        "value": value,
        "source": source,
        "necessary": necessary,
        "confidence": confidence,
        "freshness": freshness,
        "quality": quality,
    }


def _cap_pct_for(ticker: str, ips: Any) -> tuple[float, bool]:
    """Return ``(cap_pct, resolved_from_ips)`` for a ticker's concentration
    cap. Mirrors decision_funnel.stage1_routing._cap_for. ``resolved`` is
    True when the IPS supplied a real (resolved or policy_default) value;
    False when we fell back to the module constant. Either way the returned
    value is usable — a policy_default IPS field and the local fallback carry
    the SAME confidence (HIGH): both are known, documented policy, not a
    forecast."""
    is_nvda = ticker.upper() == "NVDA"
    if ips is not None:
        field = ips.nvda_cap_pct if is_nvda else ips.general_single_name_cap_pct
        if getattr(field, "value", None) is not None and getattr(field, "status", None) in (
            "resolved", "policy_default",
        ):
            return float(field.value), True
    return (
        (_FALLBACK_NVDA_CAP_PCT if is_nvda else _FALLBACK_GENERAL_SINGLE_NAME_CAP_PCT),
        False,
    )


def _sanctioned_us_situs(ips: Any) -> frozenset[str]:
    if ips is not None and getattr(ips, "sanctioned_us_situs", None):
        return frozenset(s.upper() for s in ips.sanctioned_us_situs)
    return _SANCTIONED_US_SITUS


def _estate_safe(ticker: str) -> bool | None:
    """True=non-US-situs (domicile-safe), False=US-situs, None=unknown
    instrument. Delegates to the curated instrument_reference table — the
    single source of truth for situs (see estate_safe_for docstring)."""
    try:
        from argosy.services.instrument_reference import estate_safe_for

        return estate_safe_for(ticker)
    except Exception:  # noqa: BLE001 — never break derivation on an import hiccup
        return None


def _is_etf(ticker: str) -> bool | None:
    try:
        from argosy.services.instrument_reference import STRUCT_ETF, lookup

        ref = lookup(ticker)
        return ref.structure == STRUCT_ETF if ref is not None else None
    except Exception:  # noqa: BLE001
        return None


def _is_duplicated_by_core(ticker: str, held_set: set[str]) -> bool:
    return (
        ticker.upper() in _MEGA_CAP_DUPLICATE_CANDIDATES
        and bool(held_set & _CORE_INDEX_HOLDINGS)
    )


@dataclass
class _ClassificationResult:
    verdict: str
    target_weight_pct: float | None
    target_shares: int | None
    decision_basis: str  # CONSTRAINT | FORECAST | MIXED
    binding_rules: list[str]
    decision_inputs: list[dict[str, Any]]
    action_conviction: str
    forecast_confidence: str | None


def _numeric_target(
    matched_targets: list[dict[str, Any]],
) -> tuple[float | None, int | None]:
    """Pull the strongest numeric weight/share target hint — same extraction
    the old single-path classifier used."""
    target_weight_pct: float | None = None
    target_shares: int | None = None
    for t in matched_targets:
        unit = (t.get("unit") or "").lower()
        label = (t.get("label") or "").lower()
        value = t.get("value")
        if not isinstance(value, (int, float)):
            continue
        if (
            unit == "pct_of_portfolio"
            or "share of portfolio" in label
            or "% of portfolio" in label
            or "weight" in label and "ratio" not in label
        ):
            if target_weight_pct is None:
                target_weight_pct = float(value)
        elif unit == "shares" or "share count" in label or "share ceiling" in label:
            if target_shares is None:
                target_shares = int(value)
    return target_weight_pct, target_shares


def _classify_position(
    ticker: str,
    current_weight_pct: float | None,
    matched_targets: list[dict[str, Any]],
    matched_actions: list[dict[str, Any]],
    held_set: set[str],
    ips: Any,
    forecast_confidence: str,
) -> _ClassificationResult:
    """Decide verdict + decision_basis/binding_rules/action_conviction for one
    held ticker. Order matters — most specific / hardest constraint first:

      1. US-situs + duplicated-by-core (unsanctioned) -> SELL, target 0.
      2. Explicit numeric plan sizing target -> band verdict off it.
      3. Policy concentration cap breach -> TRIM to the cap.
      4. Domicile-safe instrument with no action cue -> default HOLD (the
         "boring correct-sized UCITS core" case — HIGH without a forecast).
      5. Action-label cue (sell/trim/buy words) -> FORECAST-basis verdict.
      6. No signal at all -> FORECAST-basis HOLD, LOW.
    """
    upper = ticker.upper()
    inputs: list[dict[str, Any]] = []
    if current_weight_pct is not None:
        inputs.append(_di(
            "current_weight_pct", round(current_weight_pct, 3),
            "portfolio_snapshot", confidence="HIGH",
        ))

    # ---- 1. US-situs + duplicated-by-core -------------------------------
    safe = _estate_safe(upper)
    sanctioned = upper in _sanctioned_us_situs(ips)
    if safe is False and not sanctioned and _is_duplicated_by_core(upper, held_set):
        inputs.append(_di("estate_safe", False, "instrument_reference.estate_safe_for", confidence="HIGH"))
        inputs.append(_di("duplicated_by_core", True, "per_position_thesis._is_duplicated_by_core", confidence="HIGH"))
        return _ClassificationResult(
            verdict="SELL",
            target_weight_pct=0.0,
            target_shares=None,
            decision_basis="MIXED",  # destination from policy, timing from tax
            binding_rules=["US_SITUS", "DUPLICATE", "TARGET_ZERO"],
            decision_inputs=inputs,
            action_conviction="HIGH",
            forecast_confidence=None,  # irrelevant — the swap is policy, not alpha
        )

    # ---- 2. Explicit numeric plan sizing target --------------------------
    target_weight_pct, target_shares = _numeric_target(matched_targets)
    if (
        target_weight_pct is not None
        and current_weight_pct is not None
        and current_weight_pct > 0
    ):
        etf = _is_etf(upper)
        domicile_tag = safe is True and etf is not False
        inputs.append(_di(
            "plan_target_weight_pct", target_weight_pct,
            "horizon_json.targets", confidence="HIGH",
        ))
        rel = (target_weight_pct - current_weight_pct) / current_weight_pct
        if target_weight_pct <= 1e-9:
            verdict = "SELL"
            band_tag = "TARGET_ZERO"
        elif rel >= _ADD_TRIGGER_DRIFT_REL:
            verdict = "ADD"
            band_tag = "BELOW_MIN"
        elif rel <= _TRIM_TRIGGER_DRIFT_REL:
            verdict = "TRIM"
            band_tag = "ABOVE_MAX"
        else:
            verdict = "HOLD"
            band_tag = "IN_BAND"
        binding_rules = (
            ["DOMICILE_OK", "ROLE_MATCH", band_tag] if domicile_tag
            else ["SIZING_BAND", band_tag]
        )
        return _ClassificationResult(
            verdict=verdict,
            target_weight_pct=target_weight_pct,
            target_shares=target_shares,
            decision_basis="CONSTRAINT",
            binding_rules=binding_rules,
            decision_inputs=inputs,
            action_conviction="HIGH",
            forecast_confidence=None,
        )

    # ---- 3. Policy concentration cap breach -------------------------------
    # Single-name concentration risk is a STOCK concept (the general/NVDA cap
    # exists to bound idiosyncratic single-company risk) — never applied to a
    # diversified fund (a cash-sleeve ETF like SGOV, or a core index ETF like
    # CSPX, legitimately sits at any weight the plan wants). Skip the cap
    # check outright for a confirmed ETF; apply it to a stock or an
    # unclassified ticker (fail toward the conservative/flagged side).
    cap_pct, cap_resolved = _cap_pct_for(upper, ips)
    etf_for_cap = _is_etf(upper)
    if (
        etf_for_cap is not True
        and current_weight_pct is not None
        and current_weight_pct > cap_pct + 1e-9
    ):
        inputs.append(_di(
            "policy_cap_pct", cap_pct,
            "ips.nvda_cap_pct" if upper == "NVDA" else "ips.general_single_name_cap_pct",
            confidence="HIGH",
        ))
        return _ClassificationResult(
            verdict="TRIM",
            target_weight_pct=cap_pct,
            target_shares=None,
            decision_basis="CONSTRAINT",
            binding_rules=["POLICY_CAP_BREACH"],
            decision_inputs=inputs,
            action_conviction="HIGH",
            forecast_confidence=None,
        )

    # ---- Action-label cues (used by both 4b and 5) ------------------------
    label_blob = " ".join(
        (a.get("label") or "") + " " + (a.get("detail") or "")
        for a in matched_actions
    ).lower()
    sell_cues = ("liquidate", "exit", "sell all", "close position", "close out")
    trim_cues = (
        "deconcentrat", "reduce", "trim", " sell ", "sale", "down to",
        "tighten", "tighter", "scale back",
    )
    buy_cues = ("redeploy", "add ", "buy ", "accumulate", "increase", "grow")
    has_cue = any(
        cue in label_blob for cue in (*sell_cues, *trim_cues, *buy_cues)
    )

    # ---- 4. Known-role ETF, no action cue -> default HOLD ------------------
    # Two sub-cases, both CONSTRAINT/HIGH — a diversified fund with no signal
    # at all ("nothing changed, still fits its role") is not an abstention,
    # unlike a single stock with no thesis (that falls through to step 6):
    #   (a) confirmed estate-safe (UCITS/Israeli) core/satellite ETF, or
    #   (b) a recognized legacy US-domiciled ETF the plan already has a
    #       documented UCITS-swap note for (SGOV, SCHD, ...) — situs-exposed,
    #       so NOT tagged DOMICILE_OK, but its role/size is still a settled
    #       constraint, not a blind opinion. The swap note (prepended to
    #       reasoning_md elsewhere) carries the situs caveat.
    etf_default_hold = safe is True or upper in _US_DOMICILED_UCITS_SWAP
    if etf_default_hold and not has_cue:
        rules = ["DOMICILE_OK", "ROLE_MATCH", "IN_BAND"] if safe is True else ["ROLE_MATCH", "IN_BAND"]
        inputs.append(_di("estate_safe", safe, "instrument_reference.estate_safe_for", confidence="HIGH"))
        return _ClassificationResult(
            verdict="HOLD",
            target_weight_pct=None,
            target_shares=None,
            decision_basis="CONSTRAINT",
            binding_rules=rules,
            decision_inputs=inputs,
            action_conviction="HIGH",
            forecast_confidence=forecast_confidence,  # N/A-equivalent; recorded, not load-bearing
        )

    # ---- 5. Action-label cue, no numeric target — FORECAST/MIXED ----------
    if has_cue:
        if any(cue in label_blob for cue in sell_cues):
            verdict = "SELL"
        elif any(cue in label_blob for cue in trim_cues):
            verdict = "TRIM"
        elif any(cue in label_blob for cue in buy_cues):
            verdict = "BUY"
        else:  # pragma: no cover — has_cue implies one of the above matched
            verdict = "HOLD"
        inputs.append(_di(
            "forecast_confidence", forecast_confidence,
            "per_position_thesis._forecast_confidence_from_analysts", confidence=forecast_confidence,
        ))
        basis = "MIXED" if safe is True else "FORECAST"
        rules = ["THESIS_DRIVEN"]
        if safe is True:
            rules = ["DOMICILE_OK", "ROLE_MATCH", "THESIS_DRIVEN"]
        return _ClassificationResult(
            verdict=verdict,
            target_weight_pct=None,
            target_shares=None,
            decision_basis=basis,
            binding_rules=rules,
            decision_inputs=inputs,
            action_conviction=_min_conviction("HIGH", forecast_confidence),
            forecast_confidence=forecast_confidence,
        )

    # ---- 6. No signal at all -----------------------------------------------
    # Permitted-speculative satellite: a small, unsanctioned US-situs single
    # name (or unknown-domicile ticker) within the general cap and with no
    # plan directive at all. The constraint (size is allowed) is HIGH; the
    # THESIS (does it earn the slot) is a forecast call — action_conviction
    # floors to forecast_confidence per the min(...) rule.
    # Gated on ``safe is False`` (a KNOWN, curated US-situs single name) —
    # NOT ``safe is None`` (unclassified ticker). An instrument we can't even
    # classify is genuinely "no signal" (step 6's WAIT path below), not a
    # deliberately-permitted speculative satellite.
    is_speculative_satellite = (
        current_weight_pct is not None
        and current_weight_pct <= cap_pct
        and safe is False
    )
    inputs.append(_di(
        "forecast_confidence", forecast_confidence,
        "per_position_thesis._forecast_confidence_from_analysts", confidence=forecast_confidence,
    ))
    if is_speculative_satellite:
        inputs.append(_di("size_within_general_cap", True, "ips.general_single_name_cap_pct", confidence="HIGH"))
        return _ClassificationResult(
            verdict="HOLD",
            target_weight_pct=None,
            target_shares=None,
            decision_basis="MIXED",
            binding_rules=["SPECULATIVE_PERMITTED", "SIZE_WITHIN_CAP"],
            decision_inputs=inputs,
            action_conviction=_min_conviction("HIGH", forecast_confidence),
            forecast_confidence=forecast_confidence,
        )
    return _ClassificationResult(
        verdict="HOLD",
        target_weight_pct=None,
        target_shares=None,
        decision_basis="FORECAST",
        binding_rules=["NO_SIGNAL"],
        decision_inputs=inputs,
        action_conviction=_min_conviction("HIGH", forecast_confidence),
        forecast_confidence=forecast_confidence,
    )


def _domicile_swap_note(ticker: str) -> str | None:
    """Estate-tax domicile-swap explanation for a held US-domiciled ETF, or None."""
    twin = _US_DOMICILED_UCITS_SWAP.get(ticker.upper())
    if not twin:
        return None
    return (
        f"Plan reduces {ticker.upper()} only to migrate to its UCITS twin "
        f"{twin} for estate-tax reasons (US-domiciled ETF shares are US-situs for "
        f"a non-US-person; the Irish UCITS is not). This is a DOMICILE swap that "
        f"preserves the same economic exposure — {ticker.upper()} itself is sound; "
        f"it is not a momentum or fundamental sell."
    )


@dataclass
class PositionThesis:
    """One per-position card.

    All fields are JSON-serializable scalars or simple lists so the
    route layer can pass them straight through pydantic without an
    extra round of model declarations.
    """

    ticker: str
    current_shares: float | None
    current_weight_pct: float | None
    current_usd_value: float | None
    verdict: str  # HOLD | BUY | TRIM | SELL | ADD | WAIT
    # ACTION conviction: confidence the exact action (verdict/size/timing) is
    # correct NOW — never confidence about the future. Maps to
    # verdicts.conviction. See the module-level "Constraint-vs-forecast
    # conviction model" block: action_conviction = min(rule_confidence,
    # confidence of every NECESSARY decision input, ...). A CONSTRAINT-basis
    # card (policy cap, domicile, sizing band) can be HIGH with an unknowable
    # return; a FORECAST-basis card floors to forecast_confidence.
    conviction: str  # HIGH | MEDIUM | LOW  (== action_conviction)
    reasoning_md: str
    cited_sources: list[str] = field(default_factory=list)
    target_weight_pct: float | None = None
    target_shares: int | None = None
    # Confidence in the expected-return / fair-value / thesis call. None when
    # not applicable (a pure CONSTRAINT action never touches this) or "N/A".
    forecast_confidence: str | None = None
    # CONSTRAINT | FORECAST | MIXED — what kind of reasoning produced the
    # verdict. See argosy.services.per_position_thesis module docstring.
    decision_basis: str = "FORECAST"
    # Rule ids that produced the action, e.g. ["POLICY_CAP_BREACH"] or
    # ["DOMICILE_OK", "ROLE_MATCH", "IN_BAND"].
    binding_rules: list[str] = field(default_factory=list)
    # Necessary inputs consulted, each {"name","value","source","necessary",
    # "confidence","freshness","quality"} — the audit trail behind
    # action_conviction.
    decision_inputs: list[dict[str, Any]] = field(default_factory=list)
    # Spec C commit #6 / §6.3 — soft annotation surfaced for each
    # contributing source (currently just ``internal_news_signal_analyst``
    # which is the per-position-thesis derivation's primary upstream
    # sentiment input). Per spec: "if news_signal_analyst's reliability
    # for the last 30d is < 0.7 effective weight, the per-position
    # derivation downweights its sentiment input by that factor when
    # blending with horizon-target data. Conservative tilt: a low-
    # reliability sentiment doesn't get to FLIP a HOLD to BUY/SELL
    # alone." Shape: list of dicts produced by
    # ``argosy.services.predictions.reliability.reliability_annotation``.
    # Empty when the caller didn't provide a session (the function
    # signature stays lazy — annotations are best-effort metadata).
    reliability_annotations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# A "ticker" mention has to match a stand-alone uppercase token so we
# don't match "AS" inside "AS WE NOTED" etc. This is the same trick the
# concentration agent uses for ticker-extraction.
_TICKER_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9.\-]{0,9})(?![A-Z0-9])")


def _mentions(text: str, ticker: str) -> bool:
    """True iff ``ticker`` appears as a stand-alone token in ``text``.

    Case-insensitive but anchored on word boundaries so ``SGOV`` doesn't
    match against ``MSGOVT``. Both the ticker and the text are upper-
    cased before the substring check.
    """
    if not text or not ticker:
        return False
    upper = text.upper()
    tk = ticker.upper()
    # Quick reject — if the ticker isn't even a substring, skip the regex.
    if tk not in upper:
        return False
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(tk)}(?![A-Z0-9])")
    return bool(pattern.search(upper))


def _extract_candidate_tickers(text: str) -> set[str]:
    """Pull plausible ticker symbols out of free-form text.

    Filters out a small stop-list of all-caps English words that aren't
    tickers (US, IT, OK, NO, OR, BE, ...). Captures 1-10 char uppercase
    tokens; UCITS tickers like ``CSPX``, ``XEON``, ``FWRA`` all fit.

    Callers MUST still intersect the result with a real instrument
    universe (held ∪ plan-named ∪ instrument_reference) before creating
    ADD cards — the stop-list alone cannot catch prose acronyms like
    IPS / FIRE / TIPS.
    """
    if not text:
        return set()
    tokens = _TICKER_RE.findall(text)
    return {t for t in tokens if t.upper() not in _STOP_WORDS}


def _add_ticker_universe(
    held: set[str],
    allowed_symbols: set[str] | frozenset[str] | None = None,
) -> set[str]:
    """Real-instrument gate for ADD-card candidates.

    Universe = held snapshot symbols ∪ plan/classification extras ∪
    ``instrument_reference.known_symbols()``. Anything outside this set
    (IPS, UCITS, FIRE, …) is prose noise, not a position.
    """
    from argosy.services.instrument_reference import known_symbols

    universe: set[str] = {s.upper() for s in held if s} | set(known_symbols())
    if allowed_symbols:
        universe |= {
            (s or "").strip().upper()
            for s in allowed_symbols
            if (s or "").strip()
        }
    return universe


# Common all-caps words that match the ticker regex but aren't tickers.
# Tuned against the actual horizon JSONs in db/argosy.db so we don't
# strip out real tickers.
_STOP_WORDS: frozenset[str] = frozenset({
    "A", "AN", "AND", "OR", "BUT", "IF", "AS", "AT", "BY", "FOR",
    "FROM", "IN", "INTO", "ON", "OF", "OUT", "TO", "UP", "WITH",
    "BE", "DO", "IS", "IT", "NO", "NOT", "OK", "SO", "US", "VS",
    "WE", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
    "GMT", "EST", "PST", "PT", "ET", "AM", "PM", "Q1", "Q2", "Q3", "Q4",
    "USD", "EUR", "NIS", "GBP", "ILS", "JPY", "CHF", "CAD", "AUD",
    "FX", "ETF", "RSU", "PE", "PB", "EPS", "P", "E", "B", "PT",
    "RED", "AMBER", "YELLOW", "GREEN", "HIGH", "MEDIUM", "LOW",
    "ESG", "AML", "KYC", "API", "JSON", "TSV", "CSV", "URL",
    "ASAP", "TBD", "TODO", "FYI",
    "SDD", "WTI", "PCE", "CPI", "GDP", "VIX",
    "POA", "IRA", "401K", "529",
    "ON", "NEXT", "PRIOR", "LAST", "ALL", "ANY", "NEW",
    "BUY", "SELL", "HOLD", "TRIM", "ADD",  # verdict words
    "MA", "MACD", "RSI", "ATR",  # technical indicator names
    "MSCI", "SPDR",  # index brand prefixes that AREN'T themselves tickers
    "RISK", "OFF", "TACTICAL", "STRATEGIC",
})


def _scan_horizon_for_ticker(
    horizon_payload: dict[str, Any],
    ticker: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return (matching_targets, matching_actions, rationale_snippets).

    A target/action matches the ticker when its label OR detail OR
    rationale field contains the ticker as a word boundary token.
    """
    matched_targets: list[dict[str, Any]] = []
    matched_actions: list[dict[str, Any]] = []
    rationale_snippets: list[str] = []

    for t in horizon_payload.get("targets") or []:
        if not isinstance(t, dict):
            continue
        blob = " ".join(
            str(t.get(k, ""))
            for k in ("label", "rationale", "source_section")
        )
        if _mentions(blob, ticker):
            matched_targets.append(t)
            if t.get("rationale"):
                rationale_snippets.append(str(t["rationale"]))

    for a in horizon_payload.get("actions") or []:
        if not isinstance(a, dict):
            continue
        blob = " ".join(
            str(a.get(k, ""))
            for k in ("label", "detail", "rationale")
        )
        if _mentions(blob, ticker):
            matched_actions.append(a)
            if a.get("rationale"):
                rationale_snippets.append(str(a["rationale"]))

    return matched_targets, matched_actions, rationale_snippets


def _forecast_confidence_from_analysts(
    analyst_reports: Iterable[dict[str, Any]],
    ticker: str,
) -> str:
    """Majority-vote analyst confidence for one ticker — this is
    ``forecast_confidence`` (alpha/return-call confidence), NOT
    ``action_conviction``. Do NOT feed this directly into a CONSTRAINT-basis
    verdict's conviction (see ``_classify_position``); it is only a floor
    input for FORECAST/MIXED-basis verdicts.

    For each analyst row whose ``response_text`` mentions the ticker,
    contribute its confidence (``HIGH`` / ``MEDIUM`` / ``LOW``). Then:

      * If HIGH outnumbers everything else => HIGH.
      * Else if LOW is strictly dominant => LOW.
      * Else MEDIUM (the default for "no data" or "mixed").

    Tickers with zero analyst mentions also resolve to LOW since we have
    no evidence to back any forecast — the "we're flying blind on this
    one" signal.
    """
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    matched_any = False
    for row in analyst_reports:
        text = row.get("response_text") or ""
        if not _mentions(text, ticker):
            continue
        matched_any = True
        conf = (row.get("confidence") or "").upper()
        if conf in counts:
            counts[conf] += 1
        else:
            # Unknown / NULL confidence — bucket as MEDIUM (the default).
            counts["MEDIUM"] += 1
    if not matched_any:
        return "LOW"
    high, medium, low = counts["HIGH"], counts["MEDIUM"], counts["LOW"]
    if high > medium + low:
        return "HIGH"
    if low > high + medium:
        return "LOW"
    return "MEDIUM"


def _collect_cited_sources(
    analyst_reports: Iterable[dict[str, Any]],
    ticker: str,
) -> list[str]:
    """Return source_ids from analyst sources_json rows that mention the ticker.

    Each ``sources_json`` row is a list of ``{source_id, content}``
    dicts. We accept the source if either the ``source_id`` itself
    or the ``content`` body mentions the ticker as a token (case-
    insensitive; ``source_id`` like ``indicators/NVDA`` matches the
    substring path).
    """
    out: list[str] = []
    seen: set[str] = set()
    for row in analyst_reports:
        raw = row.get("sources_json")
        if not raw:
            continue
        try:
            sources = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(sources, list):
            continue
        for s in sources:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("source_id") or "")
            content = str(s.get("content") or "")
            # ``source_id`` uses ``/`` segments so a plain ".upper() in" check
            # is OK; for ``content`` we use the word-boundary _mentions guard.
            if not sid:
                continue
            if (
                ticker.upper() in sid.upper()
                or _mentions(content, ticker)
            ):
                if sid not in seen:
                    seen.add(sid)
                    out.append(sid)
    return out


def _looks_like_raw_data(text: str) -> bool:
    """True when ``text`` is a machine-data blob (JSON / indicator dump) rather
    than human prose.

    Guards the card reasoning against leaking raw analyst payloads like
    ``{"SCHD": {"indicators": {"rsi_14": 54.62, ...}}, "sources":
    ["yfinance:SCHD:1d"]}`` — the unreadable text the user saw. A long-hold
    investor's card should never show momentum-indicator JSON. Heuristic, not a
    parser: prose has few structural chars; a data blob is dense with
    ``{}[]":`` and key-value patterns.
    """
    s = (text or "").strip()
    if not s:
        return False
    # Telltale machine tokens (indicator keys / data-source locators).
    for needle in ('":', "rsi_14", "macd", "yfinance:", "indicators", "ma_50",
                   "1d\"", "ohlc", "{\""):
        if needle in s.lower() if needle.islower() else needle in s:
            # Only the JSON-structural ones are decisive on their own.
            if needle in ('":', "{\"", "yfinance:"):
                return True
    structural = sum(s.count(c) for c in '{}[]":')
    # >8% structural chars => not prose (a typical sentence is ~1-2%).
    return len(s) > 0 and structural / len(s) > 0.08


def _assemble_reasoning(
    ticker: str,
    rationale_snippets: list[str],
    analyst_reports: Iterable[dict[str, Any]],
) -> str:
    """Pick the strongest 2-3 snippets and join into markdown.

    Strategy: take horizon rationale strings first (they're already
    distilled), then a short excerpt (~120 chars) from each analyst row
    that mentions the ticker. Dedupe trivial duplicates. Hard-cap at
    ``_REASONING_CAP_CHARS`` so the UI doesn't overflow.
    """
    pieces: list[str] = []
    seen_prefixes: set[str] = set()

    def _add(text: str) -> None:
        s = (text or "").strip()
        if not s:
            return
        prefix = s[:60].lower()
        if prefix in seen_prefixes:
            return
        seen_prefixes.add(prefix)
        pieces.append(s)

    for snippet in rationale_snippets:
        _add(snippet)

    for row in analyst_reports:
        text = row.get("response_text") or ""
        if not _mentions(text, ticker):
            continue
        # Skip analysts whose payload is a raw data/JSON dump (e.g. a
        # technical-indicator blob) — that's the unreadable text the user
        # saw, and momentum data shouldn't drive a long-hold investor's card.
        if _looks_like_raw_data(text):
            continue
        # Pull a short window around the first ticker mention so the
        # excerpt is contextual.
        upper = text.upper()
        idx = upper.find(ticker.upper())
        if idx < 0:
            continue
        start = max(0, idx - 40)
        end = min(len(text), idx + 200)
        excerpt = text[start:end].strip()
        if excerpt and not _looks_like_raw_data(excerpt):
            # Map internal agent_role to a user-friendly label so the UI
            # doesn't see "(fundamentals_analyst)" verbatim. See
            # argosy/services/plain_english_labels.py.
            from argosy.services.plain_english_labels import friendly_agent_role
            role = friendly_agent_role(row.get("agent_role"))
            _add(f"({role}) … {excerpt} …")

    blob = "\n\n".join(pieces[:4])
    if len(blob) > _REASONING_CAP_CHARS:
        # Cut at the last whitespace before the cap so we don't break a word.
        cut = blob.rfind(" ", 0, _REASONING_CAP_CHARS - 1)
        if cut < 0:
            cut = _REASONING_CAP_CHARS - 1
        blob = blob[:cut].rstrip() + "…"
    return blob


def _ticker_to_position(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group positions by ticker, summing across accounts.

    The portfolio TSV has multiple rows for the same symbol (e.g. NVDA
    at Schwab and NVDA at Leumi); we aggregate so the card shows one
    consolidated holding per ticker. Cash / real-estate rows with no
    symbol are skipped.
    """
    out: dict[str, dict[str, Any]] = {}
    for p in positions:
        sym = (p.get("symbol") or "").strip().upper()
        # Reuse the same sentinel convention as plan_synthesis/inputs.py
        # _summarize_positions: drop the cash sentinel and any symbol-less row
        # so the two surfaces never disagree on which tickers are advisable.
        if not sym or sym == "-":
            continue
        shares = p.get("shares")
        usd_value_k = p.get("usd_value_k")
        rec = out.setdefault(sym, {"ticker": sym, "shares": 0.0, "usd_value_k": 0.0})
        if isinstance(shares, (int, float)):
            rec["shares"] += float(shares)
        if isinstance(usd_value_k, (int, float)):
            rec["usd_value_k"] += float(usd_value_k)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _load_allowed_symbols(
    session: Any,
    user_id: str,
    plan_version: Any,
) -> set[str]:
    """Plan instruments ∪ instrument_plan_classes — extras for the ADD universe."""
    out: set[str] = set()
    try:
        from argosy.services.allocation_breakdown import _plan_symbol_labels
        from argosy.services.target_allocation_doc import load_plan_target_allocation

        doc = load_plan_target_allocation(plan_version)
        out |= {s.upper() for s in _plan_symbol_labels(doc)}
    except Exception:  # noqa: BLE001 — never block thesis derivation
        logger.warning("plan-named symbols load failed", exc_info=True)
    try:
        from argosy.services.instrument_plan_class import load_classification_map

        out |= {s.upper() for s in load_classification_map(session, user_id)}
    except Exception:  # noqa: BLE001
        logger.warning("instrument_plan_classes symbols load failed", exc_info=True)
    return out


def derive_position_theses(
    plan_version: Any,
    portfolio_snapshot: Any,
    agent_reports: list[Any] | list[dict[str, Any]],
    *,
    session: Any = None,
    user_id: str | None = None,
    allowed_symbols: set[str] | frozenset[str] | None = None,
) -> list[PositionThesis]:
    """Derive one thesis card per held ticker, plus "should add" cards.

    Args:
        plan_version: An object with ``horizon_short_json``,
            ``horizon_medium_json``, ``horizon_long_json`` attributes —
            typically a ``PlanVersion`` ORM row. Mapping/dict input is
            also accepted (useful for tests).
        portfolio_snapshot: Object with a ``positions`` attribute (or
            ``["positions"]`` key) — typically a ``PortfolioSnapshot``
            pydantic model. Positions must expose ``symbol`` /
            ``shares`` / ``usd_value_k``.
        agent_reports: Iterable of ``AgentReport`` ORM rows or dicts.
            Each must expose ``response_text``, ``confidence``,
            ``agent_role``, and ``sources_json`` (string or already-
            parsed list).
        session: optional SQLAlchemy session. When provided WITH
            ``user_id``, the function queries the predictions ledger's
            ``source_reliability`` view once and attaches a per-thesis
            ``reliability_annotations`` list (spec C commit #6 / §6.3).
            Best-effort: any failure is logged and the annotations
            list stays empty so the primary derivation path is
            unaffected. Also used (with ``user_id``) to load plan-named
            / classification symbols into the ADD universe when
            ``allowed_symbols`` is omitted.
        user_id: tenant id; required when ``session`` is provided
            (multi-tenant ready per SDD §12.5). Defaults to None
            → annotations stay empty.
        allowed_symbols: optional extra symbols treated as real
            instruments (plan-named tickers / ``instrument_plan_classes``
            keys). Unioned with held snapshot symbols and
            ``instrument_reference.known_symbols()`` before any ADD
            card is created — prose acronyms outside that universe are
            dropped. When omitted and ``session``+``user_id`` are set,
            loaded automatically from the plan + classification map.

    Returns:
        list[PositionThesis] sorted so current holdings come first
        (by ``current_usd_value`` desc), with "should add" cards
        appended at the end.
    """

    if (
        allowed_symbols is None
        and session is not None
        and user_id
    ):
        allowed_symbols = _load_allowed_symbols(session, user_id, plan_version)

    # ---- Normalize inputs --------------------------------------------------
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    horizon_payloads: dict[str, dict[str, Any]] = {}
    for h_key, json_attr in (
        ("short", "horizon_short_json"),
        ("medium", "horizon_medium_json"),
        ("long", "horizon_long_json"),
    ):
        raw = _get(plan_version, json_attr)
        if not raw:
            continue
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            logger.warning("plan_version.%s is not valid JSON; skipping", json_attr)
            continue
        if isinstance(payload, dict):
            horizon_payloads[h_key] = payload

    # Portfolio positions — accept either a pydantic snapshot, a dict, or
    # a list of position dicts directly.
    raw_positions = _get(portfolio_snapshot, "positions", portfolio_snapshot)
    positions_norm: list[dict[str, Any]] = []
    if isinstance(raw_positions, list):
        for p in raw_positions:
            if isinstance(p, dict):
                positions_norm.append(p)
            else:
                # pydantic model — pull the attrs we need.
                positions_norm.append({
                    "symbol": getattr(p, "symbol", "") or "",
                    "shares": getattr(p, "shares", None),
                    "usd_value_k": getattr(p, "usd_value_k", None),
                })

    held_map = _ticker_to_position(positions_norm)

    total_usd_value_k = sum(
        rec.get("usd_value_k") or 0.0 for rec in held_map.values()
    )
    if total_usd_value_k <= 0:
        # Fall back to the snapshot's reported total if available so
        # current_weight_pct is still meaningful even when individual
        # rows had missing values.
        total_usd_value_k = float(
            _get(portfolio_snapshot, "total_usd_value_k", 0.0) or 0.0
        )

    # Analyst reports — normalize ORM rows into dicts so the helpers
    # don't have to do ``getattr`` everywhere.
    analyst_norm: list[dict[str, Any]] = []
    for row in agent_reports or []:
        if isinstance(row, dict):
            analyst_norm.append(row)
        else:
            analyst_norm.append({
                "agent_role": getattr(row, "agent_role", "") or "",
                "response_text": getattr(row, "response_text", "") or "",
                "confidence": getattr(row, "confidence", None),
                "sources_json": getattr(row, "sources_json", None),
            })

    # ---- Reliability annotations (spec C commit #6 / §6.3) ----------------
    # Spec §6.3: the per-position derivation surfaces a reliability hint
    # for ``internal_news_signal_analyst`` (the primary upstream
    # sentiment source). Same annotation attached to every PositionThesis
    # in the call — the "global" hint context applies to all tickers.
    # Computed ONCE here (cheap dict-build + cached view query) and
    # propagated via the shared list so test assertions on identity
    # don't fight us.
    shared_annotations: list[dict[str, Any]] = []
    if session is not None and user_id:
        try:
            from argosy.services.predictions.reliability import (
                reliability_annotation,
            )
            shared_annotations.append(
                reliability_annotation(
                    session,
                    user_id,
                    "internal_news_signal_analyst",
                    method_family="fixed_lookahead",
                )
            )
        except Exception:  # noqa: BLE001 — never break derivation
            logger.exception(
                "per_position_thesis: reliability_annotation failed; "
                "falling through with empty annotations list"
            )

    # ---- IPS (best-effort; None => module fallback constants) -------------
    # Live policy caps / sanctioned-situs list for the constraint branches of
    # _classify_position. Never blocks derivation — a build failure just
    # means the fallback constants (13% NVDA / 10% general) are used, same
    # as the decision_funnel's own IPS-pending behavior.
    ips: Any = None
    if session is not None and user_id:
        try:
            from argosy.services.ips import build_ips

            ips = build_ips(session, user_id=user_id)
        except Exception:  # noqa: BLE001 — never break derivation
            logger.warning("per_position_thesis: build_ips failed", exc_info=True)

    held_set = set(held_map.keys())

    # ---- Held tickers ------------------------------------------------------
    held_cards: list[PositionThesis] = []
    for ticker, rec in held_map.items():
        usd_value = float(rec.get("usd_value_k") or 0.0) * 1000.0
        current_weight_pct: float | None = None
        if total_usd_value_k > 0:
            current_weight_pct = (
                (rec.get("usd_value_k") or 0.0) / total_usd_value_k * 100.0
            )

        # Aggregate across all three horizons (long carries strategic
        # ceilings, medium carries tactical tranches, short carries
        # immediate actions — all are valid signals).
        all_targets: list[dict[str, Any]] = []
        all_actions: list[dict[str, Any]] = []
        all_snippets: list[str] = []
        for h_payload in horizon_payloads.values():
            ts, acs, snips = _scan_horizon_for_ticker(h_payload, ticker)
            all_targets.extend(ts)
            all_actions.extend(acs)
            all_snippets.extend(snips)

        forecast_confidence = _forecast_confidence_from_analysts(analyst_norm, ticker)
        result = _classify_position(
            ticker, current_weight_pct, all_targets, all_actions,
            held_set, ips, forecast_confidence,
        )
        verdict = result.verdict
        conviction = result.action_conviction
        cited = _collect_cited_sources(analyst_norm, ticker)
        reasoning = _assemble_reasoning(ticker, all_snippets, analyst_norm)
        # If this is a US-domiciled ETF the plan swaps for a UCITS twin, lead with
        # the estate-tax domicile reason so the card never reads as a momentum sell.
        swap_note = _domicile_swap_note(ticker)
        if swap_note:
            reasoning = (swap_note + ("\n\n" + reasoning if reasoning else "")).strip()

        # Abstention costs something (2026-08-21): a plain "we have no
        # opinion" LOW HOLD (no CONSTRAINT basis, no forecast evidence) is
        # relabeled WAIT rather than published as a silent HOLD — this is
        # the exact 31-blind-LOW-HOLD pattern the conviction-model rewrite
        # targets. A MIXED "permitted speculative" HOLD (SPECULATIVE_PERMITTED
        # — e.g. a small satellite name) is NOT downgraded: its slot is
        # explicitly provisional, so we say so instead of hiding it.
        if (
            verdict == "HOLD"
            and conviction == "LOW"
            and result.binding_rules == ["NO_SIGNAL"]
        ):
            verdict = "WAIT"
            note = (
                "**WAIT — insufficient evidence:** no plan target, no policy "
                "constraint, and no analyst coverage for this ticker. This is "
                "an abstention, not a considered HOLD; it will be revisited "
                "once evidence exists.\n\n"
            )
            reasoning = (note + reasoning).strip()
        elif result.binding_rules == ["SPECULATIVE_PERMITTED", "SIZE_WITHIN_CAP"]:
            note = (
                "**Provisional — 30-day review:** size is permitted within "
                "the general single-name cap, but this slot must be earned "
                "by its own thesis; re-checked within 30 days.\n\n"
            )
            reasoning = (note + reasoning).strip()

        if len(reasoning) > _REASONING_CAP_CHARS:
            cut = reasoning.rfind(" ", 0, _REASONING_CAP_CHARS - 1)
            reasoning = reasoning[: cut if cut > 0 else _REASONING_CAP_CHARS - 1].rstrip() + "…"

        held_cards.append(PositionThesis(
            ticker=ticker,
            current_shares=float(rec.get("shares") or 0.0) or None,
            current_weight_pct=(
                round(current_weight_pct, 2)
                if current_weight_pct is not None else None
            ),
            current_usd_value=round(usd_value, 2) if usd_value else None,
            verdict=verdict,
            conviction=conviction,
            reasoning_md=reasoning,
            cited_sources=cited,
            target_weight_pct=result.target_weight_pct,
            target_shares=result.target_shares,
            forecast_confidence=result.forecast_confidence,
            decision_basis=result.decision_basis,
            binding_rules=result.binding_rules,
            decision_inputs=result.decision_inputs,
            reliability_annotations=list(shared_annotations),
        ))

    # Sort held cards by USD value descending so the user sees the
    # biggest positions first.
    held_cards.sort(
        key=lambda c: (c.current_usd_value or 0.0),
        reverse=True,
    )

    # ---- "Should add" tickers ---------------------------------------------
    universe = _add_ticker_universe(held_set, allowed_symbols)
    candidate_tickers: set[str] = set()
    for h_payload in horizon_payloads.values():
        for a in h_payload.get("actions") or []:
            if not isinstance(a, dict):
                continue
            text = " ".join(
                str(a.get(k, "")) for k in ("label", "detail", "rationale")
            )
            candidate_tickers |= _extract_candidate_tickers(text)
        for t in h_payload.get("targets") or []:
            if not isinstance(t, dict):
                continue
            text = " ".join(
                str(t.get(k, "")) for k in ("label", "rationale")
            )
            candidate_tickers |= _extract_candidate_tickers(text)

    # Universe gate: prose acronyms (IPS, UCITS, FIRE, TIPS, …) match the
    # ticker regex but are not instruments — drop anything outside
    # held ∪ plan-named ∪ instrument_reference before creating ADD cards.
    add_candidates = sorted((candidate_tickers & universe) - held_set)
    add_cards: list[PositionThesis] = []
    for ticker in add_candidates:
        all_targets: list[dict[str, Any]] = []
        all_actions: list[dict[str, Any]] = []
        all_snippets: list[str] = []
        for h_payload in horizon_payloads.values():
            ts, acs, snips = _scan_horizon_for_ticker(h_payload, ticker)
            all_targets.extend(ts)
            all_actions.extend(acs)
            all_snippets.extend(snips)
        # Skip tickers that only appeared inside a stop-word-flanked
        # rationale — i.e., no real action targeted them.
        if not all_actions and not all_targets:
            continue
        forecast_confidence = _forecast_confidence_from_analysts(analyst_norm, ticker)
        cited = _collect_cited_sources(analyst_norm, ticker)
        reasoning = _assemble_reasoning(ticker, all_snippets, analyst_norm)
        # A "should-add" candidate is not held today, so there is no
        # POLICY_CAP_BREACH / sizing-band to check — it's plan-named
        # (PLAN_NAMED_ADD) and its action_conviction is the rule confidence
        # that the plan named it (HIGH) floored by whatever forecast
        # evidence exists, same min(...) rule as everywhere else.
        add_cards.append(PositionThesis(
            ticker=ticker,
            current_shares=None,
            current_weight_pct=None,
            current_usd_value=None,
            verdict="ADD",
            conviction=_min_conviction("HIGH", forecast_confidence),
            reasoning_md=reasoning,
            cited_sources=cited,
            target_weight_pct=None,
            target_shares=None,
            forecast_confidence=forecast_confidence,
            decision_basis="MIXED",
            binding_rules=["PLAN_NAMED_ADD"],
            decision_inputs=[
                _di(
                    "forecast_confidence", forecast_confidence,
                    "per_position_thesis._forecast_confidence_from_analysts",
                    confidence=forecast_confidence,
                ),
            ],
            reliability_annotations=list(shared_annotations),
        ))

    return held_cards + add_cards


def emit_thesis_predictions(
    session: "Any",
    user_id: str,
    *,
    plan_version_id: int | None,
    theses: list[PositionThesis],
    event_at: "datetime | None" = None,
    provenance_weights_applied: bool = False,
) -> None:
    """Spec C commit #3 — fan-out one prediction row per thesis card.

    Per spec §2.4 / codex BLOCKER #3 (anti-hide-behind-HOLD), every
    thesis card is logged as a prediction including HOLDs (HOLD →
    direction='neutral', still scored against subsequent price action).
    Caller passes the session + plan_version_id (used as thesis_id for
    dedup); ``derive_position_theses`` doesn't take a session today, so
    the call-site (route handler / synthesis driver) invokes both:

        theses = derive_position_theses(...)
        emit_thesis_predictions(session, user_id, plan_version_id=pv.id, theses=theses)

    Best-effort: any per-card failure is logged + swallowed so a writer
    issue never breaks the thesis-derivation primary path.

    Args:
      session: live SQLAlchemy Session. Caller owns the outer transaction;
        this function commits inline so per-card failures don't lose
        prior writes.
      user_id: tenant id (FK to users.id).
      plan_version_id: the PlanVersion row's id; used as the stable
        ``thesis_id`` component of the dedup key
        ``v1|predictions|thesis|<plan_version_id>.<ticker>``. ``None``
        skips the emit entirely (no stable id ⇒ no dedup-safe write).
      theses: the cards returned by ``derive_position_theses``.
      event_at: when the synthesis run produced these cards. Defaults to
        wallclock UTC ``now()`` if missing — the call-site SHOULD pass
        the synthesis run's completion timestamp for correct entry-price
        anchoring per spec §2.3.
      provenance_weights_applied: Spec C commit #6 / spec §6.6. When
        True, every emitted prediction is stamped with
        ``provenance_weights_applied = 1`` so downstream consumers know
        the synth has ALREADY applied upstream-source reliability
        weights to the signals that produced these theses (the
        synthesizer's prompt was given the per-source weight banner).
        Pass True from the synth-completion call site; leave False on
        the route handler (positions GET) where theses are re-derived
        on-demand without a fresh synth run.
    """
    if plan_version_id is None:
        return
    try:
        from datetime import datetime as _dt, timezone as _tz
        from argosy.services.predictions.writers import (
            write_per_position_thesis_prediction,
        )
    except Exception:  # pragma: no cover — import-guard
        logger.exception("emit_thesis_predictions: import failed")
        return

    when = event_at if event_at is not None else _dt.now(_tz.utc)
    for card in theses:
        try:
            # The thesis-derivation verdict "ADD" maps to BUY at write
            # time — ADD is the "should-add" variant of BUY and the
            # writer's action enum doesn't have a separate ADD entry.
            # Translate at the call-site so the writer's enum stays
            # closed.
            action = card.verdict
            if action == "ADD":
                action = "BUY"
            if action not in ("BUY", "TRIM", "SELL", "HOLD"):
                # Unknown verdict (defensive against future cards) —
                # skip rather than mis-classify into the ledger.
                continue
            # Note: target_weight_pct on a PositionThesis is a
            # PORTFOLIO ALLOCATION weight (e.g. "NVDA should be 45% of
            # portfolio"), NOT a ticker price target. The predictions
            # ledger's target_price column is a price level; the two
            # don't map directly. We leave target_price=None here so
            # the writer picks fixed_lookahead_30d (the correct method
            # for direction-only predictions per spec §3.1).
            # SAVEPOINT-wrapped so a writer FK / CHECK failure rolls
            # back ONLY this card, never the outer caller's session.
            with session.begin_nested():
                write_per_position_thesis_prediction(
                    session,
                    user_id,
                    thesis_id=plan_version_id,
                    ticker=card.ticker,
                    action=action,  # type: ignore[arg-type]
                    conviction=card.conviction,  # type: ignore[arg-type]
                    event_at=when,
                    target_price=None,
                    stop_price=None,
                    provenance_weights_applied=provenance_weights_applied,
                )
            session.commit()
        except Exception:  # noqa: BLE001 — never break the batch
            logger.exception(
                "emit_thesis_predictions: write failed for ticker=%s",
                card.ticker,
            )


__all__ = ["PositionThesis", "derive_position_theses", "emit_thesis_predictions"]
