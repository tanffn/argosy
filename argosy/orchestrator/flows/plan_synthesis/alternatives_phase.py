"""Alternatives sleeve subflow: source -> verify -> debate -> decide.

A single phase that returns an :class:`AlternativesSleeveDecision` for the plan
synthesis flow to thread into the canonical allocation. Pure (no DB): the caller
(``run_synthesis``) owns persistence of the agent reports + decision phase, where
the DecisionRun context lives.

Gating (codex E2E #6): hard gates FIRST — only deterministically-verified,
estate-clean candidates proceed; if none survive verification the phase
short-circuits to a 0% decision WITHOUT running the debate. A non-zero sleeve
requires source + verifier + at least two reviewer lenses; the fund manager may
still land on 0% / insufficient_data.
"""
from __future__ import annotations

from argosy.agents.alternatives_reviewers import (
    AltExposureStructureAnalyst,
    AltMacroDiversificationAnalyst,
    AltRiskLiquidityTaxAnalyst,
    AlternativesFundManagerAgent,
)
from argosy.agents.alternatives_sourcer import AlternativesProposal, AlternativesSourcerAgent
from argosy.logging import get_logger
from argosy.services.alternatives_sourcing import verify_and_gate_proposal
from argosy.services.alternatives_types import AlternativesSleeveDecision
from argosy.services.retirement.sigma_calibration import compute_alternatives_sigma

log = get_logger(__name__)

# Defensive sleeve cap (% of book). The fund manager is instructed to keep the
# sleeve small; this clamps a runaway value so the engine never receives an
# oversized sleeve. Not a policy on what the team may source — a safety rail.
_SLEEVE_HARD_CAP_PCT = 4.0
_MIN_REVIEWERS_FOR_NONZERO = 2

_SOURCING_CONSTRAINTS = (
    "HARD CONSTRAINT: every instrument MUST be NON-US-domiciled (no US-situs "
    "estate exposure) — prefer Irish/EU/Swiss/Jersey UCITS/ETC/ETP; give each "
    "instrument's domicile + ISIN + a source. The TEAM decides the sleeve size "
    "and every instrument/tilt; the user supplies no tickers and is not consulted."
)


def _zero(decision: str, rationale: str, violations: list[str]) -> AlternativesSleeveDecision:
    return AlternativesSleeveDecision(
        target_pct=0.0, sleeve_sigma=0.0, instruments=[], decision=decision,
        rationale_md=rationale, violations=violations,
    )


# --- monkeypatchable agent seams (tests patch these; no live LLM in unit tests) -
def _run_sourcer(user_id: str, macro_context: dict) -> AlternativesProposal:
    report = AlternativesSourcerAgent(user_id=user_id).run_sync(
        macro_context=macro_context, sleeve_pct=3.0, constraints=_SOURCING_CONSTRAINTS,
    )
    return report.output


def _run_reviewers(user_id: str, verified: list, macro_context: dict) -> list:
    out = []
    for cls in (
        AltExposureStructureAnalyst,
        AltMacroDiversificationAnalyst,
        AltRiskLiquidityTaxAnalyst,
    ):
        try:
            report = cls(user_id=user_id).run_sync(
                verified_candidates=verified, macro_context=macro_context
            )
            out.append(report.output)
        except Exception as exc:  # noqa: BLE001 — a dead reviewer must not kill the phase
            log.warning("alternatives_phase.reviewer_failed", role=cls.agent_role, error=str(exc))
    return out


def _run_fund_manager(user_id: str, verified: list, reviews: list, macro_context: dict):
    report = AlternativesFundManagerAgent(user_id=user_id).run_sync(
        verified_candidates=verified, reviews=reviews, macro_context=macro_context
    )
    return report.output


def _assemble_decision(verdict, verified: list, violations: list[str]) -> AlternativesSleeveDecision:
    """Build the final decision from the FM verdict + the VERIFIED candidates.

    The FM selects by symbol; we bind each selection to its verified candidate
    object (an unknown symbol is dropped — the FM cannot fabricate a holding).
    Weights are renormalised to 100; sleeve sigma is computed from the actually-
    selected instruments' asset classes."""
    if verdict.decision in ("0_percent", "insufficient_data") or verdict.target_pct <= 0:
        return _zero(verdict.decision, verdict.rationale_md, violations)

    by_symbol = {c.symbol.upper(): c for c in verified}
    chosen: list[tuple] = []
    for sel in verdict.selected:
        cand = by_symbol.get(sel.symbol.upper())
        if cand is not None and sel.weight_within_sleeve_pct > 0:
            chosen.append((cand, sel.weight_within_sleeve_pct))
    if not chosen:
        return _zero("insufficient_data",
                     "fund manager selected no verified candidate", violations)

    total_w = sum(w for _, w in chosen)
    instruments = []
    weighted_classes = []
    for cand, w in chosen:
        norm_w = round(100.0 * w / total_w, 4)
        instruments.append(cand.model_copy(update={"weight_within_sleeve_pct": norm_w}))
        weighted_classes.append((cand.asset_class, norm_w))

    target_pct = min(verdict.target_pct, _SLEEVE_HARD_CAP_PCT)
    if verdict.target_pct > _SLEEVE_HARD_CAP_PCT:
        violations = [*violations, f"FM target {verdict.target_pct}% clamped to "
                      f"{_SLEEVE_HARD_CAP_PCT}% (sleeve hard cap)"]
    sleeve_sigma = compute_alternatives_sigma(weighted_classes)

    return AlternativesSleeveDecision(
        target_pct=target_pct, sleeve_sigma=sleeve_sigma, instruments=instruments,
        decision=verdict.decision, rationale_md=verdict.rationale_md,
        review_summary_md=verdict.review_summary_md, violations=violations,
    )


def run_alternatives_phase(*, user_id: str, macro_context: dict) -> AlternativesSleeveDecision:
    """Run the full alternatives subflow and return the team's sleeve decision."""
    import argosy.orchestrator.flows.plan_synthesis.alternatives_phase as _self

    proposal = _self._run_sourcer(user_id, macro_context)
    verified, violations = verify_and_gate_proposal(proposal)

    if not verified:
        log.info("alternatives_phase.no_verified_candidates", violations=len(violations))
        return _zero(
            "0_percent",
            "No proposed instrument passed deterministic verification + the estate "
            "gate, so the team holds no Alternatives sleeve this run.",
            violations,
        )

    reviews = _self._run_reviewers(user_id, verified, macro_context)
    if len(reviews) < _MIN_REVIEWERS_FOR_NONZERO:
        log.warning("alternatives_phase.insufficient_reviewers", got=len(reviews))
        return _zero(
            "insufficient_data",
            "Fewer than two reviewer lenses completed; the team will not hold an "
            "un-reviewed sleeve.",
            violations,
        )

    verdict = _self._run_fund_manager(user_id, verified, reviews, macro_context)
    return _assemble_decision(verdict, verified, violations)


__all__ = ["run_alternatives_phase"]
