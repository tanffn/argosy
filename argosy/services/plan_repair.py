"""Transactional, same-row self-heal for persisted plan versions.

Reviewer text is input evidence, never an executable patch.  Every mutation in
this module is produced by a deterministic handler from structured plan data,
the numeric resolver, or an exact excerpt that is independently re-located and
proved unique.  The public service never creates a ``PlanVersion``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.agents.plan_synthesizer_types import (
    HorizonSection,
    PlanSynthesisOutput,
    Section,
    SynthesisInputs,
    SynthTarget,
)
from argosy.quality.fact_tokenizer import tokenize_text
from argosy.quality.fi_shock_qualifier import (
    qualify_reached_text,
    shock_needs_qualifiers,
)
from argosy.quality.leakage_gate import LEAKAGE_PATTERNS
from argosy.state.models import AgentReport, PlanRepairAttempt, PlanVersion

RepairTrigger = Literal["phase_56", "accept", "manual"]
RepairStatus = Literal[
    "REPAIRED", "PARTIAL_NEEDS_AUTHORING", "NO_PROGRESS", "REFUSED", "CONFLICT"
]

_BODY_FIELDS = (
    "horizon_long_json", "horizon_medium_json", "horizon_short_json",
    "horizon_long_md", "horizon_medium_md", "horizon_short_md",
    "horizon_long_md_audit", "horizon_medium_md_audit", "horizon_short_md_audit",
    "sections_json", "target_allocation_json",
)

_LONG_APPENDIX_MARKER = "\n## Appendix — Trajectory & Retirement-Age Reconciliation"


@dataclass(frozen=True)
class RepairRefusal:
    finding: str
    reason: str
    bucket: Literal["A", "B", "C"] = "A"


@dataclass
class HandlerReceipt:
    handler: str
    changed_surfaces: list[str] = field(default_factory=list)
    addressed: list[str] = field(default_factory=list)
    candidate_count: int = 0
    applied_count: int = 0
    leakage_before: int = 0
    leakage_after: int = 0
    gate_before: int = 0
    gate_after: int = 0
    gate_delta: int = 0
    reverted: bool = False


@dataclass
class PlanRepairResult:
    plan_version_id: int
    status: RepairStatus
    trigger: RepairTrigger
    base_artifact_sha256: str
    result_artifact_sha256: str
    same_plan_version: bool
    deterministic_before: dict[str, int]
    deterministic_after: dict[str, int]
    leakage_before: list[str]
    leakage_after: list[str]
    handlers: list[HandlerReceipt] = field(default_factory=list)
    refusals: list[RepairRefusal] = field(default_factory=list)
    reviewer_findings_addressed: list[str] = field(default_factory=list)


class PlanRepairRefused(RuntimeError):
    """Raised for a precondition that makes any repair unsafe."""


@dataclass(frozen=True)
class ContradictionCandidate:
    """Deterministic contradiction materialization input.

    ``winner_kind``/``loser_kind`` distinguish already-bound tokens from raw
    representations.  Dimension labels are required when two values are both
    legitimate but differ by basis, timestamp, currency, or scenario.
    """

    winner_text: str
    loser_text: str
    winner_kind: Literal["token", "raw"]
    loser_kind: Literal["token", "raw"]
    canonical_fact_key: str | None
    same_basis: bool = True
    same_timestamp: bool = True
    same_currency: bool = True
    same_scenario: bool = True
    winner_label: str | None = None
    loser_label: str | None = None
    changes_policy: bool = False


@dataclass(frozen=True)
class ContradictionResolution:
    replacements: tuple[tuple[str, str], ...] = ()
    refusal: str | None = None


def resolve_contradiction(candidate: ContradictionCandidate) -> ContradictionResolution:
    """Apply the no-majority-vote contradiction policy as a pure function."""
    if candidate.changes_policy:
        return ContradictionResolution(refusal="choosing a winner would change policy")
    dimensions_differ = not all((
        candidate.same_basis,
        candidate.same_timestamp,
        candidate.same_currency,
        candidate.same_scenario,
    ))
    if dimensions_differ:
        if not candidate.winner_label or not candidate.loser_label:
            return ContradictionResolution(
                refusal="different bases/timestamps/currencies/scenarios lack deterministic labels"
            )
        return ContradictionResolution(replacements=(
            (candidate.winner_text, f"{candidate.winner_label}: {candidate.winner_text}"),
            (candidate.loser_text, f"{candidate.loser_label}: {candidate.loser_text}"),
        ))
    if not candidate.canonical_fact_key:
        return ContradictionResolution(refusal="no single canonical owner")
    token = f"{{{{fact:{candidate.canonical_fact_key}}}}}"
    if candidate.winner_kind == "token" and candidate.loser_kind == "raw":
        return ContradictionResolution(replacements=((candidate.loser_text, token),))
    if candidate.winner_kind == "raw" and candidate.loser_kind == "token":
        return ContradictionResolution(replacements=((candidate.winner_text, token),))
    if candidate.winner_kind == candidate.loser_kind == "raw":
        return ContradictionResolution(replacements=(
            (candidate.winner_text, token), (candidate.loser_text, token),
        ))
    return ContradictionResolution()


def _artifact_hash(pv: PlanVersion) -> str:
    payload = {name: getattr(pv, name, None) for name in _BODY_FIELDS}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _leakage_fingerprints(text: str) -> tuple[list[str], int]:
    findings: list[str] = []
    total = 0
    for pattern in LEAKAGE_PATTERNS:
        count = (text or "").count(pattern)
        if count:
            findings.append(f"{pattern!r} x{count}")
            total += count
    return findings, total


def _load_output(pv: PlanVersion) -> PlanSynthesisOutput:
    if not all((pv.horizon_long_json, pv.horizon_medium_json, pv.horizon_short_json)):
        raise PlanRepairRefused("plan lacks all three structured horizon JSON surfaces")
    try:
        raw_inputs = json.loads(pv.synthesis_inputs_json or "{}")
        inputs = SynthesisInputs.model_validate(raw_inputs)
        sections = [
            Section.model_validate(item)
            for item in json.loads(pv.sections_json or "[]")
        ]
        return PlanSynthesisOutput(
            long=HorizonSection.model_validate_json(pv.horizon_long_json),
            medium=HorizonSection.model_validate_json(pv.horizon_medium_json),
            short=HorizonSection.model_validate_json(pv.horizon_short_json),
            inputs=inputs,
            sections=sections,
        )
    except Exception as exc:  # noqa: BLE001 - schema failure is a hard refusal
        raise PlanRepairRefused(f"structured plan validation failed: {exc}") from exc


def _render_output(
    session: Session, pv: PlanVersion, output: PlanSynthesisOutput,
) -> dict[str, str]:
    """Render JSON, user markdown and audit markdown from one structured value."""
    from argosy.orchestrator.flows.plan_synthesis.render import (
        _horizon_md_audit,
        _horizon_md_user,
        _strip_history_leak,
        _strip_jargon,
        render_plan_appendices,
    )

    long_md = _horizon_md_user(output.long)
    appendices = render_plan_appendices(
        output, session=session, decision_run_id=pv.decision_run_id,
    )
    if appendices:
        long_md = long_md.rstrip() + "\n\n" + appendices
    rendered = {
        "horizon_long_json": output.long.model_dump_json(),
        "horizon_medium_json": output.medium.model_dump_json(),
        "horizon_short_json": output.short.model_dump_json(),
        "horizon_long_md": _strip_history_leak(_strip_jargon(long_md)),
        "horizon_medium_md": _strip_history_leak(_strip_jargon(_horizon_md_user(output.medium))),
        "horizon_short_md": _strip_history_leak(_strip_jargon(_horizon_md_user(output.short))),
        "horizon_long_md_audit": _horizon_md_audit(output.long),
        "horizon_medium_md_audit": _horizon_md_audit(output.medium),
        "horizon_short_md_audit": _horizon_md_audit(output.short),
        "sections_json": json.dumps(
            [section.model_dump(mode="json") for section in output.sections],
            ensure_ascii=False,
        ),
        "target_allocation_json": pv.target_allocation_json or "",
    }
    return rendered


def _assign_rendered(pv: PlanVersion, rendered: dict[str, str]) -> None:
    for name, value in rendered.items():
        setattr(pv, name, value)


def _scoped_rendered_fields(
    pv: PlanVersion,
    rendered: dict[str, str],
    changed_surfaces: list[str],
) -> dict[str, str]:
    """Return only persisted fields owned by the surfaces a handler changed.

    A repair must not refresh unrelated bytes.  In particular, plan 125's
    persisted long-horizon appendices predate current renderer output; rebuilding
    them while changing one narrative sentence introduces unrelated numeric-gate
    findings.  Keep that exact suffix and replace only the long narrative body.
    """
    fields: dict[str, str] = {}
    for horizon in ("long", "medium", "short"):
        if not any(surface.startswith(f"{horizon}.") for surface in changed_surfaces):
            continue
        for suffix in ("json", "md", "md_audit"):
            name = f"horizon_{horizon}_{suffix}"
            fields[name] = rendered[name]

    if "horizon_long_md" in fields:
        old = pv.horizon_long_md or ""
        new = fields["horizon_long_md"]
        old_at = old.find(_LONG_APPENDIX_MARKER)
        new_at = new.find(_LONG_APPENDIX_MARKER)
        if old_at >= 0 and new_at >= 0:
            fields["horizon_long_md"] = new[:new_at] + old[old_at:]

    if any(surface.startswith("sections[") for surface in changed_surfaces):
        fields["sections_json"] = rendered["sections_json"]
    return fields


def _text_refs(output: PlanSynthesisOutput):
    """Yield stable surface ids plus setters for author-owned prose only."""
    for horizon in (output.long, output.medium, output.short):
        prefix = f"{horizon.horizon}"
        for attr in ("posture", "rationale"):
            yield f"{prefix}.{attr}", horizon, attr
        for index, target in enumerate(horizon.targets):
            for attr in ("label", "rationale"):
                yield f"{prefix}.targets[{index}].{attr}", target, attr
        for index, theme in enumerate(horizon.themes):
            for attr in ("label", "rationale"):
                yield f"{prefix}.themes[{index}].{attr}", theme, attr
        for index, action in enumerate(horizon.actions):
            for attr in ("label", "trigger_or_date", "detail", "rationale", "how_to", "done_when"):
                yield f"{prefix}.actions[{index}].{attr}", action, attr
    for index, section in enumerate(output.sections):
        yield f"sections[{index}].body_md", section, "body_md"


def _resolved_value(resolved: Any, key: str) -> tuple[float, str] | None:
    rv = resolved.get(key) if resolved is not None else None
    if rv is None or getattr(rv, "status", None) != "resolved" or getattr(rv, "value", None) is None:
        return None
    return float(rv.value), str(getattr(rv, "unit", ""))


def _assert_settled_policy(resolved: Any) -> None:
    expected = {
        "concentration.nvda_cap_pct": 0.13,
        "retirement.required_real_yield_pct": 0.03,
        "spend.fi_basis_nis": 300_000.0,
    }
    for key, wanted in expected.items():
        actual = _resolved_value(resolved, key)
        if actual is None or abs(actual[0] - wanted) > 1e-9:
            raise PlanRepairRefused(
                f"settled policy guard failed for {key}: expected {wanted}, got {actual}"
            )


def _protocol_candidate_count(text: str, resolved: Any) -> int:
    from argosy.quality.numeric_source_gate import check_fact_literal_should_be_token
    return len(check_fact_literal_should_be_token({"candidate": text}, resolved))


def _apply_fact_tokens(output: PlanSynthesisOutput, resolved: Any) -> HandlerReceipt:
    """Materialize proven placeholder/drift candidates atomically by fact key."""
    receipt = HandlerReceipt(handler="registry_literals_to_fact_tokens")
    pending: list[tuple[Any, str, str, str, list[Any]]] = []
    grouped: dict[str, int] = {}
    for surface_id, owner, attr in _text_refs(output):
        text = getattr(owner, attr, None)
        if not isinstance(text, str) or not text:
            continue
        receipt.candidate_count += _protocol_candidate_count(text, resolved)
        result = tokenize_text(text, resolved, horizon=surface_id)
        repaired = result.text
        # Drift candidates are deterministic only because fact_tokenizer has
        # already established a concept anchor and unit.  Re-prove the span
        # against the returned text, reject overlaps, then replace right-to-left.
        last_start = len(repaired) + 1
        for candidate in sorted(result.drift_candidates, key=lambda c: c.start, reverse=True):
            if candidate.end > last_start:
                raise PlanRepairRefused(f"overlapping drift candidates in {surface_id}")
            if repaired[candidate.start:candidate.end] != candidate.literal:
                raise PlanRepairRefused(f"drift locator changed in {surface_id}")
            canonical = _resolved_value(resolved, candidate.fact_key)
            if canonical is None:
                raise PlanRepairRefused(f"canonical key no longer resolves: {candidate.fact_key}")
            token = f"{{{{fact:{candidate.fact_key}}}}}"
            repaired = repaired[:candidate.start] + token + repaired[candidate.end:]
            grouped[candidate.fact_key] = grouped.get(candidate.fact_key, 0) + 1
            last_start = candidate.start
        for key, _literal in result.substitutions:
            grouped[key] = grouped.get(key, 0) + 1
        if repaired != text:
            pending.append((owner, attr, surface_id, repaired, result.drift_candidates))

    # One application point for the whole grouped worklist: no partially
    # tokenized plan can escape if any locator failed above.
    for owner, attr, surface_id, repaired, _drifts in pending:
        setattr(owner, attr, repaired)
        receipt.changed_surfaces.append(surface_id)
    receipt.applied_count = sum(grouped.values())
    receipt.addressed.extend(f"fact:{key}" for key in sorted(grouped))
    return receipt


def _shock_requirements(resolved: Any) -> tuple[bool, bool]:
    from argosy.services.retirement.fi_shock import (
        derive_fx_shock_inputs,
        derive_nvda_shock_inputs,
        fi_sufficiency_under_fx_shock,
        fi_sufficiency_under_shock,
    )
    shock = fx = None
    nvda_inputs = derive_nvda_shock_inputs(resolved)
    if nvda_inputs is not None:
        shock = fi_sufficiency_under_shock(**nvda_inputs)
    fx_inputs = derive_fx_shock_inputs(resolved)
    if fx_inputs is not None:
        fx = fi_sufficiency_under_fx_shock(**fx_inputs)
    return shock_needs_qualifiers(shock_result=shock, fx_shock_result=fx)


def _apply_fi_qualifiers(output: PlanSynthesisOutput, resolved: Any) -> HandlerReceipt:
    receipt = HandlerReceipt(handler="fi_affirmative_claim_qualifier")
    need_nvda, need_fx = _shock_requirements(resolved)
    for surface_id, owner, attr in _text_refs(output):
        text = getattr(owner, attr, None)
        if not isinstance(text, str) or not text:
            continue
        new = qualify_reached_text(text, need_nvda=need_nvda, need_fx=need_fx)
        if new != text:
            setattr(owner, attr, new)
            receipt.changed_surfaces.append(surface_id)
            receipt.applied_count += 1
    if receipt.applied_count:
        receipt.addressed.append("FI affirmative claims qualified in-sentence")
    return receipt


def _fmt_pct(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") + "%"


def _replace_cash_projection(text: str, cash_value: float) -> tuple[str, int]:
    patterns = (
        re.compile(r"(?i)(cash\s+(?:and|&)\s+(?:treasury\s+bills|t-bills)[^\n.!?]{0,90}?)(\d+(?:\.\d+)?\s*%)"),
        re.compile(r"(?i)(cash\s+sleeve[^\n.!?]{0,90}?)(\d+(?:\.\d+)?\s*%)"),
    )
    out = text
    count = 0
    for pattern in patterns:
        out, n = pattern.subn(lambda m: m.group(1) + _fmt_pct(cash_value), out)
        count += n
    return out, count


def _allocation_table(doc: Any) -> str:
    lines = [
        "<!-- canonical-allocation:start -->",
        "| Canonical sleeve | Target (% of full tradeable book) |",
        "|---|---:|",
    ]
    lines.extend(
        f"| {c.label} | {_fmt_pct(float(c.target_pct))} |"
        for c in doc.classes
    )
    lines.append("<!-- canonical-allocation:end -->")
    return "\n".join(lines)


def _apply_allocation_projection(output: PlanSynthesisOutput, pv: PlanVersion) -> HandlerReceipt:
    from argosy.services.target_allocation_doc import TargetAllocationDoc

    receipt = HandlerReceipt(handler="allocation_full_projection")
    if not pv.target_allocation_json:
        raise PlanRepairRefused("allocation projection has no TargetAllocationDoc")
    doc = TargetAllocationDoc.model_validate_json(pv.target_allocation_json)
    total = sum(float(c.target_pct) for c in doc.classes)
    if abs(total - 100.0) > 0.1:
        raise PlanRepairRefused(f"canonical allocation itself does not conserve: {total}")
    existing = [t for t in output.medium.targets if t.unit == "pct_of_portfolio"]
    if not existing:
        raise PlanRepairRefused("no structured allocation target supplies projection dates")
    template = existing[0]
    non_allocation = [t for t in output.medium.targets if t.unit != "pct_of_portfolio"]
    projected = [SynthTarget(
        label=c.label,
        value=float(c.target_pct),
        unit="pct_of_portfolio",
        stated_at=template.stated_at,
        revisit_after=template.revisit_after,
        rationale="Projected from the canonical TargetAllocationDoc; see the IPS table.",
        source_section="target_allocation_doc",
        snapshot_category=c.snapshot_category,
    ) for c in doc.classes]
    if [t.model_dump() for t in existing] != [t.model_dump() for t in projected]:
        output.medium.targets = non_allocation + projected
        receipt.changed_surfaces.append("medium.targets[pct_of_portfolio]")
        receipt.applied_count += len(projected)

    cash_classes = [c for c in doc.classes if "cash" in c.snapshot_category.lower()]
    if len(cash_classes) != 1:
        raise PlanRepairRefused("cash allocation class did not resolve uniquely")
    cash_value = float(cash_classes[0].target_pct)
    for surface_id, owner, attr in _text_refs(output):
        text = getattr(owner, attr, None)
        if not isinstance(text, str) or not text:
            continue
        new, n = _replace_cash_projection(text, cash_value)
        if n:
            setattr(owner, attr, new)
            receipt.changed_surfaces.append(surface_id)
            receipt.applied_count += n

    table = _allocation_table(doc)
    for index, section in enumerate(output.sections):
        if section.section_id != "ips":
            continue
        body = section.body_md
        block = re.compile(
            r"\n?<!-- canonical-allocation:start -->.*?<!-- canonical-allocation:end -->",
            re.DOTALL,
        )
        body = block.sub("", body).rstrip() + "\n\n" + table
        if body != section.body_md:
            section.body_md = body
            receipt.changed_surfaces.append(f"sections[{index}].body_md")
    receipt.addressed.append("ips_allocation_sum")
    return receipt


def _latest_reviews(session: Session, pv: PlanVersion) -> dict[str, dict[str, Any]]:
    if pv.decision_run_id is None:
        return {}
    decision_id = f"plan-synth-{pv.decision_run_id}"
    rows = session.execute(
        select(AgentReport)
        .where(
            AgentReport.decision_id == decision_id,
            AgentReport.agent_role.in_(("codex_second_opinion", "whole_artifact_reader")),
        )
        .order_by(AgentReport.id.desc())
    ).scalars().all()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.agent_role in out:
            continue
        try:
            payload = json.loads(row.response_text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            out[row.agent_role] = payload
    return out


def _replace_unique_excerpt(
    output: PlanSynthesisOutput, expected: str, replacement: str,
) -> list[str]:
    hits: list[tuple[str, Any, str]] = []
    for surface_id, owner, attr in _text_refs(output):
        value = getattr(owner, attr, None)
        if isinstance(value, str) and expected in value:
            if value.count(expected) != 1:
                raise PlanRepairRefused(f"expected excerpt is non-unique in {surface_id}")
            hits.append((surface_id, owner, attr))
    if not hits:
        raise PlanRepairRefused("reviewer excerpt did not resolve to a structured surface")
    for _surface_id, owner, attr in hits:
        setattr(owner, attr, getattr(owner, attr).replace(expected, replacement, 1))
    return [surface_id for surface_id, _owner, _attr in hits]


def _apply_contradictions(
    output: PlanSynthesisOutput, resolved: Any, reviews: dict[str, dict[str, Any]],
) -> tuple[HandlerReceipt, list[RepairRefusal]]:
    receipt = HandlerReceipt(handler="contradiction_policy")
    refusals: list[RepairRefusal] = []
    codex = reviews.get("codex_second_opinion", {})
    reader = reviews.get("whole_artifact_reader", {})

    # Token + stale literal: the canonical resolver owns US-situs exposure.
    for finding in codex.get("findings") or []:
        topic = str(finding.get("topic") or "")
        if "Conflicting US-situs estate totals" in topic:
            key = "concentration.us_situs_estate_exposure_nis"
            if _resolved_value(resolved, key) is None:
                refusals.append(RepairRefusal(topic, "canonical estate key does not resolve"))
                continue
            excerpts = [
                x for x in (finding.get("cited_synthesizer_paragraphs") or [])
                if isinstance(x, str) and "$3,029.1k" in x and f"{{{{fact:{key}}}}}" in x
            ]
            if len(excerpts) != 1:
                refusals.append(RepairRefusal(topic, "stale-literal excerpt did not resolve uniquely"))
                continue
            token = f"{{{{fact:{key}}}}}"
            located = [
                (surface_id, owner, attr)
                for surface_id, owner, attr in _text_refs(output)
                if isinstance(getattr(owner, attr, None), str)
                and getattr(owner, attr).count("$3,029.1k") == 1
                and token in getattr(owner, attr)
            ]
            if len(located) != 1:
                refusals.append(RepairRefusal(
                    topic, "stale estate literal did not resolve to exactly one structured surface"
                ))
                continue
            surface_id, owner, attr = located[0]
            value = getattr(owner, attr)
            sentence = re.compile(
                r"Estate:\s*\$3,029\.1k\b.*?\{\{fact:concentration\.us_situs_estate_exposure_nis\}\}.*?(?=\.\s|$)",
                re.DOTALL,
            )
            if len(sentence.findall(value)) != 1:
                refusals.append(RepairRefusal(topic, "stale estate sentence did not resolve uniquely"))
                continue
            setattr(owner, attr, sentence.sub(f"Estate exposure is {token}", value, count=1))
            changed = [surface_id]
            receipt.changed_surfaces.extend(changed)
            receipt.applied_count += len(changed)
            receipt.addressed.append(topic)
            # Canonical resolver already includes these US-domiciled lines;
            # remove only the exact stale missing-data assertion.
            for section in output.sections:
                stale = [
                    item for item in section.evidence.missing_data
                    if all(sym in item for sym in ("AVUV", "REET", "VHT"))
                    and "situs" in item.lower()
                ]
                for item in stale:
                    section.evidence.missing_data.remove(item)

        if "Moonshot" in topic or "moonshot" in topic:
            refusals.append(RepairRefusal(
                topic,
                "C3 requires a trade-specific rationale and rejected-alternative analysis",
                bucket="C",
            ))

    for finding in reader.get("findings") or []:
        detail = str(finding.get("detail") or "")
        if "3,378 future vested shares" in detail and "8,918" in detail:
            refusals.append(RepairRefusal(
                "NVDA 8,918 cumulative ceiling vs 3,378 future vests",
                "no canonical owner establishes whether the ceiling is current-programme or lifetime",
                bucket="B",
            ))
        elif "Two different NVIDIA prices" in detail:
            refusals.append(RepairRefusal(
                "Two current NVIDIA prices",
                "technical price lacks a timestamp; deterministic labels cannot be proved",
            ))
        elif "denominated in BOTH" in detail or "currency" in detail.lower() and finding.get("kind") == "contradiction":
            refusals.append(RepairRefusal(
                "Event currency conflict",
                "no canonical event owner or proved FX-equivalent relationship",
            ))
        elif "Execution gating for the immediate UCITS trade is inconsistent" in detail:
            cited = [x for x in finding.get("surfaces_cited") or [] if isinstance(x, str)]
            authoritative = [x for x in cited if "Subject to the holdings refresh" in x]
            if len(authoritative) != 1:
                refusals.append(RepairRefusal(detail, "known prerequisite did not resolve uniquely"))
                continue
            actions = [
                a for a in output.short.actions
                if "USD 125,800" in a.label and "CSPX" in a.label
            ]
            if len(actions) != 1:
                refusals.append(RepairRefusal(detail, "executable action did not resolve uniquely"))
                continue
            action = actions[0]
            expected = "Place three market-hours orders in a single session at the next LSE open"
            if action.detail.count(expected) != 1:
                refusals.append(RepairRefusal(detail, "expected action content missing or non-unique"))
                continue
            action.horizon_kind = "parameterized"
            action.trigger_or_date = "after the holdings refresh confirms current positions"
            action.detail = action.detail.replace(
                expected,
                "After the holdings refresh confirms current positions, place three market-hours orders in a single session at the first LSE open after confirmation",
                1,
            )
            receipt.changed_surfaces.extend((
                "short.actions[ucits_lump_sum].horizon_kind",
                "short.actions[ucits_lump_sum].trigger_or_date",
                "short.actions[ucits_lump_sum].detail",
            ))
            receipt.applied_count += 3
            receipt.addressed.append(detail)
    return receipt, refusals


def _apply_bounded_qualifiers(
    output: PlanSynthesisOutput, reviews: dict[str, dict[str, Any]], resolved: Any,
) -> tuple[HandlerReceipt, list[RepairRefusal]]:
    """One bounded, deterministic sentence patch per implicated slice."""
    receipt = HandlerReceipt(handler="bounded_single_slice_qualifiers")
    refusals: list[RepairRefusal] = []
    reader = reviews.get("whole_artifact_reader", {})
    for finding in reader.get("findings") or []:
        detail = str(finding.get("detail") or "")
        cited = [x for x in finding.get("surfaces_cited") or [] if isinstance(x, str)]
        if "retirement-age headline" in detail and "270,000" in detail:
            expected = [x for x in cited if "tested against" in x]
            if len(expected) != 1 or _resolved_value(resolved, "spend.fi_basis_nis") is None or _resolved_value(resolved, "spend.mc_central_nis") is None:
                refusals.append(RepairRefusal(detail, "basis facts or exact loser are not uniquely materializable", bucket="B"))
                continue
            replacement = (
                "The permanent-equivalent spending basis is {{fact:spend.fi_basis_nis}}; "
                "the Monte Carlo age engine uses {{fact:spend.mc_central_nis}} as its flat "
                "basis and models the remaining allowances as time-varying expense phases."
            )
            materialized_expected = expected[0].replace(
                "₪300,000", "{{fact:spend.fi_basis_nis}}"
            )
            try:
                changed = _replace_unique_excerpt(output, materialized_expected, replacement)
            except PlanRepairRefused as exc:
                try:
                    changed = _replace_unique_excerpt(output, expected[0], replacement)
                except PlanRepairRefused:
                    refusals.append(RepairRefusal(detail, str(exc), bucket="B"))
                    continue
            receipt.changed_surfaces.extend(changed)
            receipt.applied_count += len(changed)
            receipt.addressed.append(detail)
        elif "never spend the principal" in detail and "liquid drawdown" in detail:
            expected = [x for x in cited if "never spend the principal" in x]
            if len(expected) != 1:
                refusals.append(RepairRefusal(detail, "mandate excerpt is not unique", bucket="B"))
                continue
            replacement = (
                "The capital-preservation mandate limits long-run spending to portfolio "
                "returns after the explicitly modeled bridge; the bridge is a planned "
                "temporary liquid-capital drawdown, not a claim that principal is untouched "
                "in every year."
            )
            try:
                changed = _replace_unique_excerpt(output, expected[0], replacement)
            except PlanRepairRefused as exc:
                refusals.append(RepairRefusal(detail, str(exc), bucket="B"))
                continue
            receipt.changed_surfaces.extend(changed)
            receipt.applied_count += len(changed)
            receipt.addressed.append(detail)
    # The per-decision share cap is explicitly non-authoritative in the review.
    for finding in (reviews.get("codex_second_opinion", {}).get("findings") or []):
        if "per-decision NVIDIA cap" in str(finding.get("topic") or ""):
            refusals.append(RepairRefusal(
                str(finding.get("topic")),
                "approximately 914 shares is proposed, not a published canonical value",
                bucket="B",
            ))
    return receipt, refusals


def _gate_counts(session: Session, pv: PlanVersion) -> dict[str, int]:
    from argosy.api.routes.plan import _run_plan_output_gate
    verdict = _run_plan_output_gate(pv, session)
    if verdict is None:
        return {}
    return {
        check.value: len(items)
        for check, items in verdict.violations.items()
        if items
    }


def _assembled_text(session: Session, pv: PlanVersion) -> str:
    from argosy.services.assembled_artifact import assemble_plan_artifact
    from argosy.state.queries import get_current_plan, get_pending_draft
    displayed = get_pending_draft(session, pv.user_id) or get_current_plan(session, pv.user_id)
    if displayed is None or displayed.id != pv.id:
        raise PlanRepairRefused(
            "selected plan is not the active rendered surface; exact-plan leakage cannot be proved"
        )
    return assemble_plan_artifact(session, user_id=pv.user_id).full_text


def _persist_attempt(
    session: Session, pv: PlanVersion, result: PlanRepairResult,
) -> None:
    session.add(PlanRepairAttempt(
        plan_version_id=pv.id,
        user_id=pv.user_id,
        trigger=result.trigger,
        status=result.status,
        base_artifact_sha256=result.base_artifact_sha256,
        result_artifact_sha256=result.result_artifact_sha256,
        before_violations_json=json.dumps(result.deterministic_before, sort_keys=True),
        after_violations_json=json.dumps(result.deterministic_after, sort_keys=True),
        affected_fields_json=json.dumps(sorted({
            surface
            for handler in result.handlers
            if not handler.reverted
            for surface in handler.changed_surfaces
        })),
        instructions_json=json.dumps([asdict(h) for h in result.handlers]),
        refusals_json=json.dumps([asdict(r) for r in result.refusals]),
        terminal_reason=result.status,
    ))


def repair_plan_version(
    session: Session,
    *,
    plan_version_id: int,
    trigger: RepairTrigger,
    allow_current: bool = False,
    include_bounded: bool = True,
) -> PlanRepairResult:
    """Repair one persisted plan *in place*, guarded by hash and leakage CAS.

    ``allow_current`` is deliberately separate from ``trigger``.  A caller may
    say "manual" without thereby gaining authority to rewrite the accepted
    plan.  Administrative repair of a current plan must opt in explicitly.
    """
    if trigger not in ("phase_56", "accept", "manual"):
        raise ValueError(f"unsupported repair trigger: {trigger}")
    pv = session.get(PlanVersion, plan_version_id)
    if pv is None:
        raise LookupError(f"plan version {plan_version_id} not found")
    if pv.role == "current" and not allow_current:
        raise PlanRepairRefused("current plan repair requires allow_current=True")
    if pv.role not in ("draft", "current"):
        raise PlanRepairRefused(f"role={pv.role!r} is not repairable")

    base_hash = _artifact_hash(pv)
    output = _load_output(pv)
    if pv.decision_run_id is None:
        raise PlanRepairRefused("plan has no decision_run_id for canonical resolution")
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers
    resolved = resolve_plan_numbers(
        session,
        user_id=pv.user_id,
        decision_run_id=pv.decision_run_id,
        include_canonical_ages=True,
    )
    _assert_settled_policy(resolved)
    reviews = _latest_reviews(session, pv)

    baseline_text = _assembled_text(session, pv)
    leak_before, leak_count = _leakage_fingerprints(baseline_text)
    deterministic_before = _gate_counts(session, pv)
    current_gate_counts = deterministic_before
    working_output = copy.deepcopy(output)
    handlers: list[HandlerReceipt] = []
    refusals: list[RepairRefusal] = []

    def guarded_apply(
        fn: Callable[[], HandlerReceipt | tuple[HandlerReceipt, list[RepairRefusal]]]
    ) -> None:
        nonlocal working_output, leak_count, current_gate_counts
        before_output = copy.deepcopy(working_output)
        before_persisted = {
            name: getattr(pv, name, None)
            for name in _BODY_FIELDS
        }
        gate_before = sum(current_gate_counts.values())
        result = fn()
        if isinstance(result, tuple):
            receipt, new_refusals = result
            refusals.extend(new_refusals)
        else:
            receipt = result
        receipt.gate_before = gate_before
        if not receipt.changed_surfaces:
            receipt.gate_after = gate_before
            receipt.gate_delta = 0
            handlers.append(receipt)
            return
        rendered = _render_output(session, pv, working_output)
        _assign_rendered(
            pv,
            _scoped_rendered_fields(pv, rendered, receipt.changed_surfaces),
        )
        session.flush()
        candidate_text = _assembled_text(session, pv)
        leaks, count = _leakage_fingerprints(candidate_text)
        receipt.leakage_before = leak_count
        receipt.leakage_after = count
        candidate_gate_counts = _gate_counts(session, pv)
        receipt.gate_after = sum(candidate_gate_counts.values())
        receipt.gate_delta = receipt.gate_after - receipt.gate_before
        old_patterns = {item.split(" x", 1)[0] for item in _leakage_fingerprints(baseline_text)[0]}
        new_patterns = {item.split(" x", 1)[0] for item in leaks}
        leakage_regressed = count > leak_count or bool(new_patterns - old_patterns)
        gate_regressed = receipt.gate_delta > 0
        if leakage_regressed or gate_regressed:
            working_output = before_output
            # Restore the exact persisted bytes.  Re-rendering ``before_output``
            # is not a rollback: legacy/current rows can legitimately predate
            # renderer changes, so a fresh render may differ from the base row
            # and can itself introduce gate violations.
            _assign_rendered(pv, before_persisted)
            session.flush()
            receipt.reverted = True
            reasons: list[str] = []
            if leakage_regressed:
                reasons.append(
                    "edit increased leakage or introduced a new leakage fingerprint"
                )
            if gate_regressed:
                reasons.append(
                    "edit increased deterministic gate violations "
                    f"({receipt.gate_before} -> {receipt.gate_after})"
                )
            refusals.append(RepairRefusal(receipt.handler, "; ".join(reasons)))
        else:
            leak_count = count
            current_gate_counts = candidate_gate_counts
        handlers.append(receipt)

    guarded_apply(lambda: _apply_fact_tokens(working_output, resolved))
    guarded_apply(lambda: _apply_fi_qualifiers(working_output, resolved))
    guarded_apply(lambda: _apply_allocation_projection(working_output, pv))
    guarded_apply(lambda: _apply_contradictions(working_output, resolved, reviews))
    if include_bounded:
        guarded_apply(lambda: _apply_bounded_qualifiers(working_output, reviews, resolved))

    # Every accepted edit was rendered and flushed by ``guarded_apply``.  Do
    # not render again here: when all edits were reverted, an unconditional
    # render would replace the exact base bytes with the current renderer's
    # output and bypass the per-handler guards.
    session.flush()
    _load_output(pv)
    final_text = _assembled_text(session, pv)
    leak_after, final_leak_count = _leakage_fingerprints(final_text)
    if final_leak_count > _leakage_fingerprints(baseline_text)[1]:
        raise PlanRepairRefused("final leakage guard regressed")
    deterministic_after = _gate_counts(session, pv)
    result_hash = _artifact_hash(pv)

    # Compare-and-swap: refresh a second ORM view without overwriting our
    # candidate.  The DB value must still carry the hash we started from.
    db_row = session.execute(
        select(PlanVersion).where(PlanVersion.id == pv.id).execution_options(populate_existing=False)
    ).scalar_one()
    if db_row is not pv and _artifact_hash(db_row) != base_hash:
        raise PlanRepairRefused("base artifact changed concurrently")

    before_total = sum(deterministic_before.values())
    after_total = sum(deterministic_after.values())
    changed = result_hash != base_hash
    if changed and after_total < before_total:
        status: RepairStatus = "PARTIAL_NEEDS_AUTHORING" if refusals else "REPAIRED"
    elif changed and after_total <= before_total:
        status = "PARTIAL_NEEDS_AUTHORING" if refusals else "REPAIRED"
    elif not changed:
        status = "REFUSED" if refusals else "NO_PROGRESS"
    else:
        raise PlanRepairRefused(
            f"complete deterministic gate regressed: {before_total} -> {after_total}"
        )
    addressed = sorted({
        item
        for handler in handlers
        if not handler.reverted
        for item in handler.addressed
    })
    result = PlanRepairResult(
        plan_version_id=pv.id,
        status=status,
        trigger=trigger,
        base_artifact_sha256=base_hash,
        result_artifact_sha256=result_hash,
        same_plan_version=True,
        deterministic_before=deterministic_before,
        deterministic_after=deterministic_after,
        leakage_before=leak_before,
        leakage_after=leak_after,
        handlers=handlers,
        refusals=refusals,
        reviewer_findings_addressed=addressed,
    )
    _persist_attempt(session, pv, result)
    session.commit()
    session.refresh(pv)
    if pv.id != plan_version_id:
        raise AssertionError("repair changed PlanVersion identity")
    return result


__all__ = [
    "ContradictionCandidate", "ContradictionResolution", "HandlerReceipt",
    "PlanRepairRefused", "PlanRepairResult", "RepairRefusal",
    "repair_plan_version", "resolve_contradiction",
]
