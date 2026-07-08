"""Corrective (critique-fed) re-synthesis context builder.

Design: docs/design/corrective_resynthesis.md (part A).

A re-synthesis triggered BY critique findings must not start from zero — it
would plausibly reproduce the same defects. This builder turns the already-
persisted critique + reconcile outcome + accepted adjudication proposals into
STRUCTURED corrections that the orchestrator prepends to synthesis guidance
(every phase receives ``guidance`` as ``user_directive``, so the whole team
sees them) and that the post-run corrections-landed verifier
(``argosy/quality/corrections_check.py``) checks deterministically.

Sources (all already persisted; no new state):

1. Latest ``plan_critiques`` row for the user's CURRENT plan — findings + the
   embedded ``reconcile`` payload written by ``critique_reconcile``. Only
   findings whose reconcile status is ``escalated`` / ``disputed-upheld`` /
   ``unresolved`` become corrections. ``fixed`` and ``disputed-withdrawn``
   are settled and MUST NOT be re-fed (re-feeding a withdrawn finding would
   re-litigate a settled dispute).
2. The open aggregated ``replan_full`` proposal
   (``dedup_key = critique_resynth:{user_id}``) — its payload carries the
   escalated findings verbatim; used to cross-check (1) and to know which
   proposal to close on promote. Tolerates ``status='accepted'`` too (Ariel
   may confirm the proposal before the run fires).
3. Accepted-but-unapplied adjudication proposals (``status='accepted'``,
   execution_state ``proposed`` or ``accepted_pending_user_action`` — the
   value the real accept service sets — + kind in a small allowlist) —
   these become DIRECTIVES: apply verbatim, never re-decide.
4. Derived facts (``derived_facts.build_derived_facts``) — each correction is
   joined to the derived fact(s) covering its surface (lenient token match,
   same spirit as ``critique_reconcile.findings_match``) so it carries its
   canonical value, not just "this is wrong".

Fail-soft by contract: the orchestrator wraps the call; any exception here
degrades the run to today's behavior (the part-C gate is the backstop).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.services.critique_reconcile import findings_match
from argosy.state.models import ActionProposal, PlanCritique
from argosy.state.queries import get_current_plan

_log = get_logger("argosy.services.corrective_context")

# Reconcile statuses that make a finding a CORRECTION (still open after the
# reconcile loop). ``fixed`` / ``disputed-withdrawn`` are settled — never re-fed.
OPEN_RECONCILE_STATUSES = frozenset({"escalated", "disputed-upheld", "unresolved"})

# Proposal kinds whose ACCEPTED rows are adjudicated verdicts that apply at
# synthesis (directives). Deliberately small — see design §2.A.3.
DIRECTIVE_PROPOSAL_KINDS = ("update_plan_assumption", "replan_full")

_WORD_RE = re.compile(r"[a-z0-9%]+")

# Derived-fact key tokens that are units/suffixes, not subject matter — they
# never appear in finding prose and would dilute the match score.
_FACT_TOKEN_STOPWORDS = frozenset({"sh", "w", "x", "nis", "pct", "usd"})


@dataclass
class Correction:
    """One correction the corrective run must clear."""

    index: int  # 1-based, stable render order
    severity: str
    topic: str
    plan_item_ref: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    reconcile_status: str = "escalated"
    # Joined derived facts: [(fact_key, value), ...] — canonical values the
    # deterministic corrections-landed floor checks for.
    canonical_facts: list[tuple[str, Any]] = field(default_factory=list)
    # Known-wrong values that must be ABSENT from the corrected draft. The
    # live builder leaves this empty (extracting figures from finding prose is
    # not deterministic enough for a gate); tests / future structured findings
    # populate it.
    wrong_values: list[Any] = field(default_factory=list)

    def check_payload(self) -> dict[str, Any]:
        """Plain-dict form consumed by ``corrections_check`` (pure module)."""
        return {
            "index": self.index,
            "topic": self.topic,
            "plan_item_ref": self.plan_item_ref,
            "canonical_values": [v for _, v in self.canonical_facts],
            "wrong_values": list(self.wrong_values),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "severity": self.severity,
            "topic": self.topic,
            "plan_item_ref": self.plan_item_ref,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "reconcile_status": self.reconcile_status,
            "canonical_facts": [[k, v] for k, v in self.canonical_facts],
            "wrong_values": list(self.wrong_values),
        }


@dataclass
class Directive:
    """One adjudicated verdict — apply verbatim, do not re-decide."""

    index: int  # 1-based
    proposal_id: int
    kind: str
    summary: str
    detail: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "summary": self.summary,
            "detail": self.detail,
        }


@dataclass
class CorrectiveContext:
    """Structured corrections + directives, plus the rendered guidance block.

    The verifier (corrections_check) and the promote hook consume the
    STRUCTURED form (``to_payload`` → ``synthesis_inputs_json.corrective``),
    never re-parse the prose.
    """

    corrections: list[Correction] = field(default_factory=list)
    directives: list[Directive] = field(default_factory=list)
    # Proposals this run feeds — flipped to status='executed' on promote:
    # the aggregated critique_resynth proposal + every directive proposal.
    proposal_ids: list[int] = field(default_factory=list)
    source_critique_id: int | None = None
    # Prior current plan reference (the base document being edited).
    base_plan_id: int | None = None
    base_plan_label: str = ""
    # True when a correction class implicates phase-1 inputs (e.g. a
    # refresh_snapshot-routed finding) — the run must NOT reuse phases 1-2.
    forces_full_tier: bool = False
    rendered: str = ""

    def to_payload(
        self,
        *,
        reused_from_run_id: int | None = None,
        reused_phases: list[int] | None = None,
    ) -> dict[str, Any]:
        return {
            "corrections": [c.to_payload() for c in self.corrections],
            "directives": [d.to_payload() for d in self.directives],
            "proposal_ids": list(self.proposal_ids),
            "source_critique_id": self.source_critique_id,
            "base_plan_id": self.base_plan_id,
            "base_plan_label": self.base_plan_label,
            "forces_full_tier": self.forces_full_tier,
            "reused_from_run_id": reused_from_run_id,
            "reused_phases": list(reused_phases or []),
        }

    @property
    def reader_directive(self) -> str:
        """Directive block for the whole-artifact reader (part-C judgment pass)."""
        if not self.corrections and not self.directives:
            return ""
        lines = [
            "CORRECTIVE-RUN VERIFICATION — this draft exists to CLEAR the "
            "corrections below. For EACH correction, verify it is genuinely "
            "resolved IN SUBSTANCE across every surface that touches it — not "
            "cosmetically absorbed (the canonical value pasted in one spot "
            "while another surface still asserts the flagged claim). If ANY "
            "correction is not genuinely resolved, emit a BLOCKER finding "
            "citing the correction number and the offending surface excerpt.",
        ]
        for c in self.corrections:
            canon = (
                "; ".join(f"{k} = {_fmt_value(v)}" for k, v in c.canonical_facts)
                or "(no canonical derived value — judge resolution in substance)"
            )
            lines.append(
                f"[{c.index}] {c.severity} · {c.topic} · surface: "
                f"{c.plan_item_ref}\n    finding: {c.summary}\n"
                f"    canonical: {canon}"
            )
        for d in self.directives:
            lines.append(
                f"[D{d.index}] proposal #{d.proposal_id} ({d.kind}) — "
                f"{d.summary} — verify it was applied verbatim."
            )
        return "\n".join(lines)


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        if v == int(v):
            return f"{int(v):,}"
        return f"{v:g}"
    return str(v)


def _finding_words(finding: dict[str, Any]) -> set[str]:
    text = " ".join(
        str(finding.get(k) or "")
        for k in ("topic", "plan_item_ref", "summary")
    ).lower()
    return set(_WORD_RE.findall(text))


def match_fact_to_finding(fact_key: str, finding: dict[str, Any]) -> bool:
    """Lenient, deterministic derived-fact ↔ finding join.

    Same spirit as ``critique_reconcile.findings_match`` (majority token
    overlap), adapted to fact KEYS: STRICTLY more than half of the fact
    key's subject tokens (underscore-split, unit suffixes dropped) must
    appear as words in the finding's topic/ref/summary text. The strict
    bound matters because every joined fact's value must land VERBATIM in
    the draft (the deterministic floor now requires ALL canonical values)
    — a half-match join (e.g. ``nvda_breaking_sh`` attaching to any
    NVDA-mentioning finding on the ``nvda`` token alone) would 422 good
    drafts on incidental facts (codex finding #5 trade-off).
    """
    tokens = [
        t for t in fact_key.lower().split("_")
        if t and t not in _FACT_TOKEN_STOPWORDS
    ]
    if not tokens:
        return False
    words = _finding_words(finding)
    hits = sum(1 for t in tokens if t in words)
    if len(tokens) == 1:
        return hits == 1
    return hits / len(tokens) > 0.5


def _load_latest_critique(
    session: Session, *, user_id: str, plan_version_id: int
) -> PlanCritique | None:
    return session.execute(
        select(PlanCritique)
        .where(
            PlanCritique.user_id == user_id,
            PlanCritique.plan_version_id == plan_version_id,
        )
        .order_by(PlanCritique.id.desc())
        .limit(1)
    ).scalars().first()


def _load_resynth_proposal(
    session: Session, *, user_id: str
) -> ActionProposal | None:
    """The aggregated critique→re-synthesis proposal.

    ``status IN ('open','accepted')`` — Ariel may have already confirmed it
    before the corrective run fires; either way its findings are corrections
    and the row is closed on promote.
    """
    return session.execute(
        select(ActionProposal)
        .where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key == f"critique_resynth:{user_id}",
            ActionProposal.status.in_(("open", "accepted")),
        )
        .order_by(ActionProposal.id.desc())
        .limit(1)
    ).scalars().first()


def _load_directive_proposals(
    session: Session, *, user_id: str
) -> list[ActionProposal]:
    """Accepted-but-unapplied adjudication proposals (design §2.A.3).

    Excludes the aggregated ``critique_resynth:*`` row — its findings are
    CORRECTIONS (source 2), never directives.
    """
    rows = session.execute(
        select(ActionProposal)
        .where(
            ActionProposal.user_id == user_id,
            ActionProposal.status == "accepted",
            # The accept service flips execution_state to
            # 'accepted_pending_user_action' (action_proposals.py); rows
            # accepted out-of-band may still read 'proposed'. Both mean
            # accepted-but-unapplied — applied rows leave this selector
            # via status='executed' at promote, never via execution_state.
            ActionProposal.execution_state.in_(
                ("proposed", "accepted_pending_user_action")
            ),
            ActionProposal.kind.in_(DIRECTIVE_PROPOSAL_KINDS),
        )
        .order_by(ActionProposal.id.asc())
    ).scalars().all()
    return [
        r for r in rows
        if not (r.dedup_key or "").startswith("critique_resynth:")
    ]


def _render_block(ctx: CorrectiveContext) -> str:
    """Deterministic guidance block (design §2.A rendered shape)."""
    base_ref = (
        f"the prior current plan ({ctx.base_plan_label or 'current'}, "
        f"#{ctx.base_plan_id})"
        if ctx.base_plan_id is not None
        else "the prior current plan"
    )
    lines = [
        "CORRECTIVE RE-SYNTHESIS — this run exists to CLEAR the corrections "
        "below while EDITING the prior plan. You are NOT drafting from zero. "
        f"Preserve every plan element NOT implicated by a correction; {base_ref} "
        "is the base document.",
    ]
    if ctx.corrections:
        lines.append(
            "\nCORRECTIONS (each must be resolved; the post-run verifier "
            "checks each one):"
        )
        for c in ctx.corrections:
            evidence = "; ".join(c.evidence) if c.evidence else "(none)"
            canon = (
                "; ".join(
                    f"{k} = {_fmt_value(v)} (derived-fact)"
                    for k, v in c.canonical_facts
                )
                or "(no canonical derived value on file — re-derive from raw "
                "inputs and state the derivation)"
            )
            lines.append(
                f"[{c.index}] {c.severity} · {c.topic} · surface: "
                f"{c.plan_item_ref}\n"
                f"    wrong: {c.summary} — evidence: {evidence}\n"
                f"    canonical: {canon}\n"
                f"    required: the corrected surface must state the canonical "
                f"value/position and must no longer assert the flagged claim."
            )
    if ctx.directives:
        lines.append(
            "\nDIRECTIVES (adjudicated verdicts — apply verbatim, do not "
            "re-decide):"
        )
        for d in ctx.directives:
            entry = f"[D{d.index}] proposal #{d.proposal_id} — {d.summary}"
            if d.detail:
                entry += f"\n     {d.detail}"
            lines.append(entry)
    return "\n".join(lines)


def build_corrective_context(
    session: Session,
    *,
    user_id: str,
    decision_run_id: int | None = None,
) -> CorrectiveContext | None:
    """Build the corrective context, or None when nothing is open.

    Returns None when there are neither open corrections (critique findings
    with reconcile status escalated / disputed-upheld / unresolved, or the
    aggregated re-synthesis proposal's findings) nor accepted-but-unapplied
    adjudication directives — i.e. a plain re-synthesis with nothing to feed.
    """
    ctx = CorrectiveContext()

    current = get_current_plan(session, user_id)
    if current is not None:
        ctx.base_plan_id = current.id
        ctx.base_plan_label = current.version_label or ""

    # ---- Source 1: latest critique + embedded reconcile ------------------
    critique_findings: list[dict[str, Any]] = []
    reconcile: dict[str, Any] = {}
    if current is not None:
        row = _load_latest_critique(
            session, user_id=user_id, plan_version_id=current.id
        )
        if row is not None:
            try:
                payload = json.loads(row.critique_json or "{}")
            except (TypeError, ValueError):
                payload = {}
            raw_findings = payload.get("findings")
            if isinstance(raw_findings, list):
                critique_findings = [f for f in raw_findings if isinstance(f, dict)]
            raw_reconcile = payload.get("reconcile")
            if isinstance(raw_reconcile, dict):
                reconcile = raw_reconcile
                ctx.source_critique_id = row.id

    selected: list[tuple[dict[str, Any], str]] = []  # (finding, status)
    finding_status = reconcile.get("finding_status")
    if isinstance(finding_status, list):
        for i, f in enumerate(critique_findings):
            tag = finding_status[i] if i < len(finding_status) else None
            if tag in OPEN_RECONCILE_STATUSES:
                selected.append((f, str(tag)))

    # Settled subjects (codex blocker #1): anything the latest reconcile
    # marked fixed / disputed-withdrawn is CLOSED — a stale proposal payload
    # must never re-feed it (that would re-litigate a settled dispute).
    settled: list[dict[str, Any]] = []
    per_finding_rows = reconcile.get("per_finding")
    if isinstance(per_finding_rows, list):
        for pf in per_finding_rows:
            if (
                isinstance(pf, dict)
                and pf.get("status") in ("fixed", "disputed-withdrawn")
            ):
                settled.append({
                    "topic": pf.get("topic"),
                    "plan_item_ref": pf.get("plan_item_ref"),
                })

    # ---- Source 2: the aggregated re-synthesis proposal ------------------
    resynth = _load_resynth_proposal(session, user_id=user_id)
    if resynth is not None:
        ctx.proposal_ids.append(resynth.id)
        try:
            p_payload = json.loads(resynth.suggested_payload or "{}")
        except (TypeError, ValueError):
            p_payload = {}
        p_findings = p_payload.get("findings")
        if isinstance(p_findings, list):
            for f in p_findings:
                if not isinstance(f, dict):
                    continue
                if any(findings_match(f, sf) for sf, _ in selected):
                    continue  # already selected from the critique row
                if any(findings_match(f, sd) for sd in settled):
                    _log.info(
                        "corrective_context.proposal_finding_settled_skipped",
                        user_id=user_id, topic=f.get("topic"),
                    )
                    continue  # settled since the proposal was written
                selected.append((f, "escalated"))

    # ---- Source 3: accepted adjudication proposals → directives ----------
    for i, p in enumerate(_load_directive_proposals(session, user_id=user_id), 1):
        detail = (p.rationale_md or "").strip()
        if len(detail) > 2000:
            detail = detail[:2000] + " …"
        ctx.directives.append(
            Directive(
                index=i,
                proposal_id=p.id,
                kind=p.kind,
                summary=(p.summary or "").strip(),
                detail=detail,
            )
        )
        ctx.proposal_ids.append(p.id)

    if not selected and not ctx.directives:
        return None

    # ---- Source 4: derived-fact join --------------------------------------
    facts: dict[str, Any] = {}
    try:
        from argosy.services.derived_facts import build_derived_facts

        facts = build_derived_facts(
            session, user_id=user_id, decision_run_id=decision_run_id
        ) or {}
    except Exception as exc:  # noqa: BLE001 — join is best-effort
        _log.warning(
            "corrective_context.derived_facts_failed",
            user_id=user_id, error=str(exc)[:200],
        )
        facts = {}

    for i, (f, status) in enumerate(selected, 1):
        canonical = [
            (k, v) for k, v in sorted(facts.items())
            if match_fact_to_finding(k, f)
        ]
        evidence = f.get("evidence")
        if not isinstance(evidence, list):
            evidence = [str(evidence)] if evidence else []
        ctx.corrections.append(
            Correction(
                index=i,
                severity=str(f.get("severity") or "RED"),
                topic=str(f.get("topic") or ""),
                plan_item_ref=str(f.get("plan_item_ref") or ""),
                summary=str(f.get("summary") or ""),
                evidence=[str(e) for e in evidence],
                reconcile_status=status,
                canonical_facts=canonical,
            )
        )

    # Snapshot-class corrections implicate phase-1 inputs → full tier. The
    # reconcile loop tags refresh_snapshot-routed findings status='routed'.
    per_finding = reconcile.get("per_finding")
    if isinstance(per_finding, list):
        ctx.forces_full_tier = any(
            isinstance(pf, dict) and pf.get("status") == "routed"
            for pf in per_finding
        )

    ctx.rendered = _render_block(ctx)
    _log.info(
        "corrective_context.built",
        user_id=user_id,
        corrections=len(ctx.corrections),
        directives=len(ctx.directives),
        proposal_ids=ctx.proposal_ids,
        forces_full_tier=ctx.forces_full_tier,
        source_critique_id=ctx.source_critique_id,
    )
    return ctx


def upsert_open_proposal_sync(
    session: Session,
    *,
    user_id: str,
    kind: str,
    dedup_key: str,
    summary: str,
    rationale_md: str,
    payload: dict[str, Any],
    severity: str,
    now: datetime,
) -> None:
    """Sync sibling of ``critique_reconcile._upsert_action_proposal``.

    The synthesis orchestrator runs on a sync Session (often inside a worker
    thread with no event loop), so the async upsert is unusable there.
    Insert-or-refresh one open ActionProposal, idempotent per dedup_key.
    """
    existing = session.execute(
        select(ActionProposal).where(
            ActionProposal.dedup_key == dedup_key,
            ActionProposal.status == "open",
        )
    ).scalars().first()
    if existing is not None:
        existing.summary = summary
        existing.rationale_md = rationale_md
        existing.suggested_payload = json.dumps(payload)
        existing.severity = severity
        existing.surfaced_at = now
        existing.expires_at = now + timedelta(days=30)
    else:
        session.add(
            ActionProposal(
                user_id=user_id,
                summary=summary,
                rationale_md=rationale_md,
                suggested_payload=json.dumps(payload),
                severity=severity,
                surfaced_at=now,
                expires_at=now + timedelta(days=30),
                status="open",
                kind=kind,
                dedup_key=dedup_key,
                execution_state="proposed",
            )
        )
    session.commit()


__all__ = [
    "Correction",
    "CorrectiveContext",
    "Directive",
    "DIRECTIVE_PROPOSAL_KINDS",
    "OPEN_RECONCILE_STATUSES",
    "build_corrective_context",
    "match_fact_to_finding",
    "upsert_open_proposal_sync",
]
