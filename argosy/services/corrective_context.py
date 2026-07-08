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
5. VERDICT FEEDBACK — the user's most recent superseded-or-pending corrective
   draft (a ``PlanVersion`` with ``synthesis_inputs_json.corrective`` present)
   whose run ended FM-rejected or reader-blocked. The FM's rejection
   ``reasons`` and the whole-artifact reader's blocking findings become
   STRUCTURED corrections (``source='verdict_feedback'``) instead of the
   free-text paste-back a human had to do before. Explicit wrong/canonical
   figure pairs stated by the verdict (e.g. ``1,591/9,880 vs adjudicated
   9,822/1,649``) are extracted deterministically so the patch-reachability
   classifier can route the next pass PATCH; verdicts without explicit
   figures honestly route FULL. Scope-guarded (same current-plan lineage,
   newer than the latest critique row) and deduped against the critique
   corrections via ``findings_match``. Items the prior corrective run
   resolved that the verdicts did NOT re-flag render as a do-not-reopen
   list.

Fail-soft by contract: the orchestrator wraps the call; any exception here
degrades the run to today's behavior (the part-C gate is the backstop).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.services.critique_reconcile import findings_match
from argosy.state.models import (
    ActionProposal,
    AgentReport,
    DecisionRun,
    PlanCritique,
    PlanVersion,
    PortfolioSnapshotRow,
)
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
    # Known-wrong values that must be ABSENT from the corrected draft.
    # Critique-sourced corrections leave this empty (extracting figures from
    # finding prose is not deterministic enough for a gate); VERDICT-FEEDBACK
    # corrections populate it when the FM/reader stated the wrong figures
    # explicitly (``extract_verdict_figures``); tests / future structured
    # findings populate it too.
    wrong_values: list[Any] = field(default_factory=list)
    # Provenance (source 5, verdict feedback): 'critique' for reconcile-loop
    # findings; 'verdict_feedback' for corrections harvested from a prior
    # corrective draft's FM rejection / reader block. Verdict-feedback
    # corrections also carry which agent produced the verdict and the
    # source run/draft ids so payloads + rendering can distinguish them.
    source: str = "critique"
    verdict_agent: str = ""  # 'fund_manager' | 'whole_artifact_reader'
    source_run_id: int | None = None
    source_draft_id: int | None = None

    @property
    def parsed_ref(self) -> dict[str, Any]:
        """Parsed slice/item addressing of ``plan_item_ref`` (corrective
        patch-synthesis, docs/design/corrective_patch_synthesis.md §2.B).
        Deterministic; the patch-reachability classifier re-resolves against
        the prior draft — this is the carried addressing form."""
        from argosy.quality.patch_reachability import parse_plan_item_ref

        return parse_plan_item_ref(self.plan_item_ref).to_payload()

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
        payload = {
            "index": self.index,
            "severity": self.severity,
            "topic": self.topic,
            "plan_item_ref": self.plan_item_ref,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "reconcile_status": self.reconcile_status,
            "canonical_facts": [[k, v] for k, v in self.canonical_facts],
            "wrong_values": list(self.wrong_values),
            "source": self.source,
            "verdict_agent": self.verdict_agent,
            "source_run_id": self.source_run_id,
            "source_draft_id": self.source_draft_id,
        }
        # Parsed addressing carried alongside (patch-synthesis design §4);
        # best-effort — an unparseable ref degrades to nulls, never raises.
        try:
            payload["parsed_ref"] = self.parsed_ref
        except Exception:  # noqa: BLE001 — addressing is advisory
            payload["parsed_ref"] = None
        return payload


@dataclass
class Directive:
    """One adjudicated verdict — apply verbatim, do not re-decide."""

    index: int  # 1-based
    proposal_id: int
    kind: str
    summary: str
    detail: str = ""
    # Patch-synthesis addressing (docs/design/corrective_patch_synthesis.md):
    # plan-item refs the directive's verbatim application targets, plus the
    # superseded figures that must be ABSENT post-application (e.g. the old
    # glide-schedule legs proposal 49 replaces). The live builder leaves both
    # empty today (adjudication proposals are prose); structured proposals /
    # tests populate them. Empty ⇒ the patch-reachability classifier honestly
    # routes the directive to FULL (unaddressable for a scoped patch).
    target_refs: list[str] = field(default_factory=list)
    superseded_values: list[Any] = field(default_factory=list)

    def check_payload(self) -> dict[str, Any]:
        """Deterministic-floor form (codex patch-review blocker #2): a
        directive's SUPERSEDED figures must be absent from the corrected
        draft exactly like a correction's wrong values. No canonical-value
        presence check — a directive's application shape is prose; the
        reader's judgment pass owns 'applied verbatim'."""
        return {
            "index": self.index,
            "topic": f"directive #{self.proposal_id}: {self.summary[:60]}",
            "plan_item_ref": "; ".join(self.target_refs),
            "canonical_values": [],
            "wrong_values": list(self.superseded_values),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "summary": self.summary,
            "detail": self.detail,
            "target_refs": list(self.target_refs),
            "superseded_values": list(self.superseded_values),
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
    # refresh_snapshot-routed finding) AND the demanded refresh has not
    # happened since the critique (latest portfolio snapshot not newer than
    # the critique row) — the run must NOT reuse phases 1-2.
    forces_full_tier: bool = False
    # Verdict-feedback provenance (source 5): the rejected prior corrective
    # draft + run the feedback was harvested from, which verdicts fired, and
    # the items those verdicts confirmed resolved (do-not-reopen list).
    verdict_feedback_draft_id: int | None = None
    verdict_feedback_run_id: int | None = None
    verdict_fm_rejected: bool = False
    verdict_reader_blocked: bool = False
    # [{"topic", "plan_item_ref"}, ...] — corrections the prior corrective
    # draft resolved that the FM/reader did NOT re-flag.
    verdict_confirmed_resolved: list[dict[str, Any]] = field(default_factory=list)
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
            "verdict_feedback": (
                {
                    "draft_id": self.verdict_feedback_draft_id,
                    "run_id": self.verdict_feedback_run_id,
                    "fm_rejected": self.verdict_fm_rejected,
                    "reader_blocked": self.verdict_reader_blocked,
                    "confirmed_resolved": [
                        dict(item) for item in self.verdict_confirmed_resolved
                    ],
                }
                if self.verdict_feedback_draft_id is not None
                else None
            ),
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
            entry = (
                f"[D{d.index}] proposal #{d.proposal_id} ({d.kind}) — "
                f"{d.summary} — verify it was applied verbatim."
            )
            # Codex patch-review blocker #2: the reader needs the verbatim
            # detail + the superseded figures to judge "applied verbatim"
            # in substance, not just by the summary line.
            if d.detail:
                detail = d.detail if len(d.detail) <= 500 else d.detail[:500] + " …"
                lines_detail = detail.replace("\n", "\n    ")
                entry += f"\n    detail: {lines_detail}"
            if d.superseded_values:
                entry += (
                    "\n    superseded figures that must NOT survive: "
                    + "; ".join(_fmt_value(v) for v in d.superseded_values)
                )
            lines.append(entry)
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


# ---------------------------------------------------------------------------
# Source 5 — verdict feedback (FM rejection / reader block on a prior
# corrective draft). Deterministic harvest; no LLM, no re-judgment.
# ---------------------------------------------------------------------------

# Plan-item refs the FM/reader cite verbatim in verdict prose
# (``medium.targets.nvda`` / ``medium.targets`` / ``section:ips`` /
# ``long.status`` …) — the same addressing scheme the patch-reachability
# classifier resolves.
_VERDICT_REF_RE = re.compile(
    r"\b(?:(?:long|medium|short)\.(?:targets?|themes?|actions?|"
    r"speculative_candidates?|posture|rationale|status)(?:\.[A-Za-z0-9_]+)*"
    r"|sections?[.:][A-Za-z0-9_]+)\b"
)

# Figures as verdicts state them: comma-grouped ints, decimals, plain ints.
_FIGURE_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+")

# Explicit wrong-vs-canonical separators. Deliberately narrow: only phrasings
# where the LEFT side is unambiguously the draft's (wrong) figure and the
# RIGHT side the canonical one. Anything else leaves values empty and the
# classifier honestly routes the correction FULL.
_VS_RE = re.compile(
    r"\bvs\.?\b|\bversus\b|\bshould\s+(?:be|read|state)\b", re.IGNORECASE
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+")


def _figures_in(text: str) -> list[Any]:
    """Numeric figures in ``text`` — comma-stripped, int-normalized, with
    plain 4-digit calendar-year tokens (1900-2100, no comma/decimal)
    filtered out so '2026/2027' glide years are never mistaken for values."""
    out: list[Any] = []
    for token in _FIGURE_RE.findall(text or ""):
        raw = token.replace(",", "")
        try:
            v = float(raw)
        except ValueError:  # pragma: no cover — regex guarantees numeric
            continue
        if "," not in token and "." not in token and 1900 <= v <= 2100:
            continue  # calendar year, not a figure
        norm: Any = int(v) if v == int(v) else v
        if norm not in out:
            out.append(norm)
    return out


def extract_verdict_figures(text: str) -> tuple[list[Any], list[Any]]:
    """``(wrong_values, canonical_values)`` — ONLY when the verdict states
    them explicitly as a wrong-vs-canonical figure pair in one sentence
    (e.g. ``states 1,591/9,880 vs adjudicated 9,822/1,649``). Anything less
    explicit returns ``([], [])`` — the patch classifier then honestly
    routes the correction FULL rather than guessing at figures.
    """
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        parts = _VS_RE.split(sentence)
        if len(parts) != 2:
            continue  # zero or ambiguous (multiple) separators
        wrong = _figures_in(parts[0])
        canonical = _figures_in(parts[1])
        # A figure on BOTH sides is context, not a wrong value.
        wrong = [v for v in wrong if v not in canonical]
        if not wrong or not canonical:
            continue
        if len(wrong) > 6 or len(canonical) > 6:
            continue  # figure soup — not an explicit pair statement
        return wrong, canonical
    return [], []


def _verdict_topic(text: str) -> str:
    """Deterministic short topic from verdict prose (first 8 words)."""
    words = (text or "").strip().split()
    return " ".join(words[:8])[:80]


def _verdict_ref(text: str) -> str:
    """First plan-item ref the verdict text cites, or ''."""
    m = _VERDICT_REF_RE.search(text or "")
    return m.group(0).lower() if m else ""


def _loads_lenient(text: str | None) -> dict[str, Any] | None:
    """Parse a persisted agent ``response_text`` as JSON — strict first,
    then ``raw_decode`` from the first ``{`` (model chatter tolerated)."""
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (TypeError, ValueError):
        pass
    first = text.find("{")
    if first >= 0:
        try:
            obj, _ = json.JSONDecoder(strict=False).raw_decode(text[first:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    return None


def _verdict_finding(
    text: str, *, severity: str, agent: str, evidence: list[str] | None = None
) -> dict[str, Any]:
    wrong, canonical = extract_verdict_figures(text)
    return {
        "severity": severity,
        "topic": _verdict_topic(text),
        "plan_item_ref": _verdict_ref(text),
        "summary": text,
        "evidence": list(evidence or []),
        "wrong_values": wrong,
        "canonical_values": canonical,
        "verdict_agent": agent,
    }


def _load_rejected_corrective_draft(
    session: Session, *, user_id: str, current_plan_id: int
) -> tuple[PlanVersion, dict[str, Any], DecisionRun] | None:
    """The user's MOST RECENT corrective draft (superseded or pending), iff
    its run ended rejected and it belongs to the current-plan lineage.

    Decides on the most recent corrective draft ONLY: if that draft's run
    was NOT rejected (promoted / approved), older rejected verdicts are
    superseded history and must not resurrect — return None.
    """
    rows = session.execute(
        select(PlanVersion)
        .where(
            PlanVersion.user_id == user_id,
            PlanVersion.role.in_(("draft", "superseded")),
            PlanVersion.decision_run_id.is_not(None),
            PlanVersion.synthesis_inputs_json.is_not(None),
        )
        .order_by(PlanVersion.id.desc())
        .limit(25)
    ).scalars().all()
    for pv in rows:
        try:
            si = json.loads(pv.synthesis_inputs_json or "{}")
        except (TypeError, ValueError):
            continue
        corrective = si.get("corrective") if isinstance(si, dict) else None
        if not isinstance(corrective, dict):
            continue
        # Most recent corrective draft found — decide on THIS one only.
        run = session.get(DecisionRun, pv.decision_run_id)
        if (
            run is None
            or run.user_id != user_id
            or run.fund_manager_decision != "rejected"
        ):
            return None
        if corrective.get("base_plan_id") != current_plan_id:
            _log.info(
                "corrective_context.verdict_feedback_lineage_mismatch",
                user_id=user_id, draft_id=pv.id,
                draft_base_plan_id=corrective.get("base_plan_id"),
                current_plan_id=current_plan_id,
            )
            return None
        return pv, si, run
    return None


def _load_latest_verdict_report(
    session: Session, *, user_id: str, decision_run_id: int, agent_role: str
) -> AgentReport | None:
    return session.execute(
        select(AgentReport)
        .where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == f"plan-synth-{decision_run_id}",
            AgentReport.agent_role == agent_role,
        )
        .order_by(AgentReport.id.desc())
        .limit(1)
    ).scalars().first()


def _harvest_verdict_feedback(
    session: Session,
    *,
    user_id: str,
    current_plan_id: int,
    critique_created_at: datetime | None,
) -> dict[str, Any] | None:
    """Source 5: structured verdict findings from the most recent rejected
    corrective draft, or None when there is nothing (fresh) to harvest.

    Returns ``{"draft_id", "run_id", "fm_rejected", "reader_blocked",
    "findings", "confirmed_resolved"}``.
    """
    loaded = _load_rejected_corrective_draft(
        session, user_id=user_id, current_plan_id=current_plan_id
    )
    if loaded is None:
        return None
    pv, si, run = loaded

    # Staleness guard: the rejected draft must be NEWER than the latest
    # critique row — a fresh critique (with its reconcile) supersedes old
    # verdicts about a long-superseded draft. Missing draft timestamp is
    # treated as stale (fail-safe toward not resurrecting).
    crit = _as_utc_naive(critique_created_at)
    draft_ts = _as_utc_naive(pv.imported_at)
    if crit is not None and (draft_ts is None or draft_ts <= crit):
        _log.info(
            "corrective_context.verdict_feedback_stale_skipped",
            user_id=user_id, draft_id=pv.id,
            draft_imported_at=draft_ts.isoformat() if draft_ts else None,
            critique_created_at=crit.isoformat(),
        )
        return None

    findings: list[dict[str, Any]] = []
    fm_rejected = False
    reader_blocked = False

    fm_row = _load_latest_verdict_report(
        session, user_id=user_id, decision_run_id=run.id,
        agent_role="fund_manager",
    )
    if fm_row is not None:
        fm = _loads_lenient(fm_row.response_text)
        if fm is not None and fm.get("approved") is False:
            fm_rejected = True
            for reason in fm.get("reasons") or []:
                text = str(reason).strip()
                if text:
                    findings.append(_verdict_finding(
                        text, severity="RED", agent="fund_manager",
                    ))

    reader_row = _load_latest_verdict_report(
        session, user_id=user_id, decision_run_id=run.id,
        agent_role="whole_artifact_reader",
    )
    if reader_row is not None:
        rd = _loads_lenient(reader_row.response_text)
        if rd is not None and rd.get("overall_assessment") == "BLOCK":
            reader_blocked = True
            for f in rd.get("findings") or []:
                if not isinstance(f, dict):
                    continue
                sev = f.get("severity")
                if sev not in ("BLOCKER", "AMBER"):
                    continue  # YELLOW = polish, not a blocking finding
                detail = str(f.get("detail") or "").strip()
                if not detail:
                    continue
                surfaces = f.get("surfaces_cited")
                evidence = (
                    [str(s) for s in surfaces[:4]]
                    if isinstance(surfaces, list) else []
                )
                findings.append(_verdict_finding(
                    detail,
                    severity="RED" if sev == "BLOCKER" else "YELLOW",
                    agent="whole_artifact_reader",
                    evidence=evidence,
                ))

    if not findings:
        return None

    # Confirmed-resolved (do-not-reopen): corrections the prior corrective
    # run carried that the verdicts did NOT re-flag AND the deterministic
    # floor did not report unresolved — the verdicts' scope notes.
    unresolved_idx = {
        int(u.get("index") or 0)
        for u in (si.get("corrective_unresolved") or [])
        if isinstance(u, dict)
    }
    confirmed_resolved: list[dict[str, Any]] = []
    corrective = si.get("corrective") or {}
    for pc in corrective.get("corrections") or []:
        if not isinstance(pc, dict):
            continue
        if int(pc.get("index") or 0) in unresolved_idx:
            continue
        if any(findings_match(pc, f) for f in findings):
            continue
        confirmed_resolved.append({
            "topic": str(pc.get("topic") or ""),
            "plan_item_ref": str(pc.get("plan_item_ref") or ""),
        })

    _log.info(
        "corrective_context.verdict_feedback_harvested",
        user_id=user_id, draft_id=pv.id, run_id=run.id,
        fm_rejected=fm_rejected, reader_blocked=reader_blocked,
        findings=len(findings), confirmed_resolved=len(confirmed_resolved),
    )
    return {
        "draft_id": pv.id,
        "run_id": run.id,
        "fm_rejected": fm_rejected,
        "reader_blocked": reader_blocked,
        "findings": findings,
        "confirmed_resolved": confirmed_resolved,
    }


def _as_utc_naive(dt: datetime | None) -> datetime | None:
    """Normalize to naive-UTC for comparison (SQLite drops tzinfo; the
    project convention is naive == UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _snapshot_refresh_postdates_critique(
    session: Session, *, user_id: str, critique_created_at: datetime | None
) -> bool:
    """True when the user's LATEST portfolio snapshot is strictly newer than
    the source critique row — i.e. the refresh a snapshot-class finding
    demanded has already happened since the critique was taken, so forcing
    the full tier for staleness would pay ~25 min for a problem that no
    longer exists.

    Deterministic and conservative by design: ANY doubt (no critique
    timestamp, no snapshot row, unusable timestamps, query error) returns
    False — the caller keeps forcing the expensive-but-correct full run.
    """
    try:
        crit = _as_utc_naive(critique_created_at)
        if crit is None:
            return False
        latest = session.execute(
            select(PortfolioSnapshotRow.imported_at)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(PortfolioSnapshotRow.imported_at.desc())
            .limit(1)
        ).scalars().first()
        snap = _as_utc_naive(latest)
        if snap is None:
            return False
        if snap > crit:
            _log.info(
                "corrective_context.snapshot_force_waived",
                user_id=user_id,
                snapshot_imported_at=snap.isoformat(),
                critique_created_at=crit.isoformat(),
            )
            return True
        return False
    except Exception as exc:  # noqa: BLE001 — fail-safe toward forcing
        _log.warning(
            "corrective_context.snapshot_force_freshness_check_failed",
            user_id=user_id, error=str(exc)[:200],
        )
        return False


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
    critique_corrections = [
        c for c in ctx.corrections if c.source != "verdict_feedback"
    ]
    verdict_corrections = [
        c for c in ctx.corrections if c.source == "verdict_feedback"
    ]
    if critique_corrections:
        lines.append(
            "\nCORRECTIONS (each must be resolved; the post-run verifier "
            "checks each one):"
        )
        for c in critique_corrections:
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
    if verdict_corrections:
        modes = []
        if ctx.verdict_fm_rejected:
            modes.append("FM-rejected")
        if ctx.verdict_reader_blocked:
            modes.append("reader-blocked")
        mode = " / ".join(modes) if modes else "rejected"
        draft_ref = (
            f"#{ctx.verdict_feedback_draft_id}"
            if ctx.verdict_feedback_draft_id is not None else "(prior)"
        )
        lines.append(
            f"\nVERDICT FEEDBACK (prior corrective draft {draft_ref} was "
            f"{mode} on these — fix surgically, do not re-open what those "
            "verdicts confirmed resolved):"
        )
        for c in verdict_corrections:
            entry = (
                f"[{c.index}] {c.severity} · {c.topic} · surface: "
                f"{c.plan_item_ref or '(unspecified)'} · verdict: "
                f"{c.verdict_agent}\n"
                f"    finding: {c.summary}"
            )
            if c.evidence:
                entry += "\n    evidence: " + "; ".join(c.evidence)
            if c.wrong_values:
                entry += (
                    "\n    wrong (must be absent): "
                    + "; ".join(_fmt_value(v) for v in c.wrong_values)
                )
            canon_vals = [v for _, v in c.canonical_facts]
            if canon_vals:
                entry += (
                    "\n    canonical (must appear verbatim): "
                    + "; ".join(_fmt_value(v) for v in canon_vals)
                )
            else:
                entry += (
                    "\n    canonical: (the verdict stated no explicit figure "
                    "— resolve the finding in substance)"
                )
            lines.append(entry)
        if ctx.verdict_confirmed_resolved:
            lines.append(
                f"\nCONFIRMED RESOLVED in draft {draft_ref} by those verdicts "
                "— reuse that resolution; do NOT re-open or re-decide:"
            )
            for item in ctx.verdict_confirmed_resolved:
                topic = item.get("topic") or "?"
                ref = item.get("plan_item_ref") or ""
                lines.append(f"- {topic}" + (f" ({ref})" if ref else ""))
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
    critique_created_at: datetime | None = None
    if current is not None:
        row = _load_latest_critique(
            session, user_id=user_id, plan_version_id=current.id
        )
        if row is not None:
            critique_created_at = row.created_at
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

    # ---- Source 5: verdict feedback from a rejected corrective draft ------
    # Harvested BEFORE the emptiness check (verdict feedback alone is enough
    # to make a run corrective) and deduped against the critique corrections
    # via findings_match — the critique row already owns those subjects.
    verdict_findings: list[dict[str, Any]] = []
    verdict_harvest: dict[str, Any] | None = None
    if current is not None:
        try:
            verdict_harvest = _harvest_verdict_feedback(
                session,
                user_id=user_id,
                current_plan_id=current.id,
                critique_created_at=critique_created_at,
            )
        except Exception as exc:  # noqa: BLE001 — source 5 is best-effort
            verdict_harvest = None
            _log.warning(
                "corrective_context.verdict_feedback_failed",
                user_id=user_id, error=str(exc)[:200],
            )
    if verdict_harvest is not None:
        for f in verdict_harvest["findings"]:
            if any(findings_match(f, sf) for sf, _ in selected):
                continue  # the critique correction already covers it
            if any(findings_match(f, vf) for vf in verdict_findings):
                continue  # FM and reader flagged the same subject
            verdict_findings.append(f)
        if verdict_findings:
            ctx.verdict_feedback_draft_id = verdict_harvest["draft_id"]
            ctx.verdict_feedback_run_id = verdict_harvest["run_id"]
            ctx.verdict_fm_rejected = verdict_harvest["fm_rejected"]
            ctx.verdict_reader_blocked = verdict_harvest["reader_blocked"]
            ctx.verdict_confirmed_resolved = list(
                verdict_harvest["confirmed_resolved"]
            )

    # ---- Source 3: accepted adjudication proposals → directives ----------
    for i, p in enumerate(_load_directive_proposals(session, user_id=user_id), 1):
        detail = (p.rationale_md or "").strip()
        if len(detail) > 2000:
            detail = detail[:2000] + " …"
        # Structured patch addressing (codex patch-review r2, blocker #2
        # residual): a proposal whose payload carries explicit
        # target_refs / superseded_values gets them onto the directive so
        # the patch classifier can scope it and the deterministic floor
        # can verify the superseded figures are gone. Best-effort + strict
        # (only the explicit keys; never inferred from prose) — absent
        # keys leave both empty, which the classifier honestly routes to
        # the full tier.
        target_refs: list[str] = []
        superseded_values: list[Any] = []
        try:
            p_payload = json.loads(p.suggested_payload or "{}")
            if isinstance(p_payload, dict):
                raw_refs = (
                    p_payload.get("target_refs")
                    or p_payload.get("plan_item_refs")
                )
                if isinstance(raw_refs, list):
                    target_refs = [str(r) for r in raw_refs if r]
                raw_super = (
                    p_payload.get("superseded_values")
                    or p_payload.get("wrong_values")
                )
                if isinstance(raw_super, list):
                    superseded_values = [v for v in raw_super if v is not None]
        except (TypeError, ValueError):
            pass
        ctx.directives.append(
            Directive(
                index=i,
                proposal_id=p.id,
                kind=p.kind,
                summary=(p.summary or "").strip(),
                detail=detail,
                target_refs=target_refs,
                superseded_values=superseded_values,
            )
        )
        ctx.proposal_ids.append(p.id)

    if not selected and not verdict_findings and not ctx.directives:
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

    # Verdict-feedback corrections (source 5) — appended AFTER the critique
    # corrections with continuing indices. No derived-fact join: canonical
    # values come ONLY from figures the verdict stated explicitly
    # (``extract_verdict_figures``); a verdict without explicit figures
    # carries none and the patch classifier honestly routes it FULL.
    for j, f in enumerate(verdict_findings, len(ctx.corrections) + 1):
        ctx.corrections.append(
            Correction(
                index=j,
                severity=str(f["severity"]),
                topic=str(f["topic"]),
                plan_item_ref=str(f["plan_item_ref"]),
                summary=str(f["summary"]),
                evidence=[str(e) for e in f["evidence"]],
                reconcile_status="verdict_feedback",
                canonical_facts=[
                    ("verdict_stated", v) for v in f["canonical_values"]
                ],
                wrong_values=list(f["wrong_values"]),
                source="verdict_feedback",
                verdict_agent=str(f["verdict_agent"]),
                source_run_id=ctx.verdict_feedback_run_id,
                source_draft_id=ctx.verdict_feedback_draft_id,
            )
        )

    # Snapshot-class corrections implicate phase-1 inputs → full tier. The
    # reconcile loop tags refresh_snapshot-routed findings status='routed'.
    # Freshness-aware: when the user's latest portfolio snapshot POSTDATES
    # the source critique row, the demanded refresh has already happened —
    # forcing is waived (logged as corrective_context.snapshot_force_waived).
    # Any doubt keeps forcing (fail-safe toward the expensive-but-correct
    # run); the phase-1/2 reuse freshness validation in
    # _select_corrective_reuse_run still applies on top.
    per_finding = reconcile.get("per_finding")
    if isinstance(per_finding, list):
        snapshot_routed = any(
            isinstance(pf, dict) and pf.get("status") == "routed"
            for pf in per_finding
        )
        ctx.forces_full_tier = snapshot_routed and not (
            _snapshot_refresh_postdates_critique(
                session,
                user_id=user_id,
                critique_created_at=critique_created_at,
            )
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
        verdict_feedback_draft_id=ctx.verdict_feedback_draft_id,
        verdict_feedback_run_id=ctx.verdict_feedback_run_id,
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
    "extract_verdict_figures",
    "match_fact_to_finding",
    "upsert_open_proposal_sync",
]
