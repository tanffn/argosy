"""Fleet self-review detectors — deterministic anomaly catchers.

Each detector is a pure function:

    def detect_XYZ(db: Session, scope: ReviewScope) -> list[Finding]

No LLM call. No mutation. Cheap to run (~seconds total for all ten).
Detection is deterministic so the user can trust the findings aren't
hallucinated — the LLM composition step in the runner ONLY wraps a
human-readable preamble around the deterministic list.

Why detectors are pure functions rather than methods on a class:
  * Each detector is independent.  Adding D11 / D12 later is just a new
    function in this module and a registration line in ``ALL_DETECTORS``.
  * Tests can target one detector at a time with a synthetic DB.
  * A buggy detector can't corrupt sibling detectors' results.

Defensive contract — EVERY detector body is wrapped in try/except in
``run_all_detectors``.  A detector that can't run (missing data, schema
mismatch, unexpected JSON shape) MUST NOT crash the report — it logs
the failure and returns an empty list.

This file is the OUTPUT of the lessons learned from commit f8faaca,
where ``run_synthesis(guidance=...)`` accepted user feedback for many
waves but the orchestrator dropped it at the parameter boundary.  The
``guidance_pipeline_no_op`` detector (D1) catches exactly that class
of bug via AST inspection — a parameter declared but never referenced.
"""

from __future__ import annotations

import ast
import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import AgentReport, DecisionPhase, DecisionRun

log = get_logger(__name__)


Severity = Literal["RED", "AMBER", "YELLOW"]


@dataclass
class Finding:
    """One anomaly surfaced by one detector.

    Attributes:
      id: stable string id (``<detector>:<short-natural-key>``) so a
          downstream UI / dedup pass can spot the same finding across
          consecutive reports.
      detector: which detector emitted this finding (D1 .. D10 today).
      severity: RED (must fix), AMBER (should investigate), YELLOW
          (informational drift).
      category: free-form bucket — ``"architecture"`` /
          ``"behavior"`` / ``"data_quality"`` / ``"reliability"`` /
          ``"cost"``.  Used to group sections of the markdown report.
      title: one-line headline shown in the report's table of contents.
      evidence: structured citations — DB rows, file:line refs,
          measured numbers.  No prose claim is made here that the
          ``evidence`` field doesn't contain literal data backing it.
      suggested_fix: a one-line nudge for the developer.  Not a
          patch — the detector does NOT know how to fix the bug; this
          is the cheapest hint about WHERE to look.
    """

    id: str
    detector: str
    severity: Severity
    category: str
    title: str
    evidence: dict = field(default_factory=dict)
    suggested_fix: str = ""


@dataclass
class ReviewScope:
    """Inputs to a self-review run.

    Attributes:
      user_id: the user whose runs/agents we're inspecting.  All queries
          filter on this so the review is per-tenant by design.
      decision_run_id: when set, the review is post-synthesis for THIS
          run; detectors may use it to focus on the most-recent run
          rather than the whole history.  When None, this is a daily
          sweep — detectors look back ``lookback_days``.
      lookback_days: rolling window for daily-sweep detectors
          (D2, D6, D9).  Default 14 days strikes a balance between
          "enough samples to see a pattern" and "fresh enough to catch
          a current regression".
      orchestrator_path: absolute path to the synthesis orchestrator
          source file.  D1 reads this file directly; in tests, point at
          a fixture path that contains the bad shape to exercise D1.
    """

    user_id: str
    decision_run_id: int | None = None
    lookback_days: int = 14
    orchestrator_path: Path | None = None


# ----------------------------------------------------------------------
# D1 — guidance_pipeline_no_op
# ----------------------------------------------------------------------


def _default_orchestrator_path() -> Path:
    from argosy.config import get_settings

    settings = get_settings()
    # Repo root is the parent of argosy/ package.  settings.home is
    # ARGOSY_HOME (where db + logs live) — distinct from the repo root.
    # The orchestrator file lives in the repo, so derive from this
    # module's __file__.
    return (
        Path(__file__).resolve().parent.parent
        / "orchestrator" / "flows" / "plan_synthesis" / "orchestrator.py"
    )


def detect_guidance_pipeline_no_op(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D1 — declared-but-unused parameter on a phase function.

    Walks the orchestrator's AST.  For every top-level function whose
    name starts with ``_run_phase_``, check whether the parameter list
    contains ``guidance``.  If it does, the function body MUST reference
    the name ``guidance`` at least once (a real read, not just the
    parameter declaration).  If not — exactly the bug fixed in
    commit f8faaca — emit a RED finding.

    This is the architectural detector class — it inspects source code,
    not runtime data, so it catches the issue BEFORE the user has to
    notice "the FM keeps rejecting on the same theme".

    Generalises to any param name (not hard-coded to ``guidance``) so
    when the spec adds e.g. ``user_directive`` we can call this twice.
    """
    path = scope.orchestrator_path or _default_orchestrator_path()
    out: list[Finding] = []
    if not path.exists():
        log.info("fleet_self_review.d1_skipped_no_orchestrator", path=str(path))
        return out

    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError) as exc:
        log.warning(
            "fleet_self_review.d1_parse_failed",
            path=str(path), error=str(exc),
        )
        return out

    # Names whose presence in a phase signature must be matched by an
    # actual read inside the body.  Extend this tuple to add new
    # tracked parameters.
    TRACKED = ("guidance", "user_directive")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_run_phase_"):
            continue
        # Collect every declared arg name (positional, keyword-only, etc.)
        declared = [
            a.arg for a in (
                list(node.args.args)
                + list(node.args.kwonlyargs)
                + list(node.args.posonlyargs)
            )
        ]
        for tracked_name in TRACKED:
            if tracked_name not in declared:
                continue
            # Walk the function body and look for ANY Name node
            # referring to the parameter.  An attribute access like
            # ``obj.guidance`` doesn't count — that's a different name.
            referenced = False
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Name)
                    and child.id == tracked_name
                    and not isinstance(child.ctx, ast.Store)
                ):
                    # The parameter declaration itself doesn't appear as
                    # a ast.Name with ctx=Load — it's an ast.arg.  So
                    # any Name match is a real read/use.
                    referenced = True
                    break
            if not referenced:
                out.append(Finding(
                    id=f"D1:{node.name}:{tracked_name}",
                    detector="D1",
                    severity="RED",
                    category="architecture",
                    title=(
                        f"Phase function `{node.name}` declares "
                        f"parameter `{tracked_name}` but never uses it"
                    ),
                    evidence={
                        "file": str(path),
                        "function": node.name,
                        "line": node.lineno,
                        "param": tracked_name,
                    },
                    suggested_fix=(
                        f"Thread `{tracked_name}` into the prompt / "
                        f"agent call inside `{node.name}`, OR drop it "
                        f"from the signature if not needed.  This is "
                        f"the same shape as commit f8faaca."
                    ),
                ))
    return out


# ----------------------------------------------------------------------
# D2 — consecutive_fm_rejections_same_theme
# ----------------------------------------------------------------------

_FM_REASON_TOPIC_SPLIT = re.compile(r"[:.\-—–]")


def _normalise_topic(text: str) -> str:
    """Reduce an FM-reason string to a comparable short topic token.

    Strategy: take the first ~12 words, lowercase, strip punctuation.
    The FM tends to lead each reason with a noun phrase that names
    the concern (e.g. "Cross-horizon coherence failure...", "Section
    102 tax sequencing...", "ConcentrationAnalyst null positions...").
    Keeping the first dozen words preserves enough signal to detect
    recurrence across runs without being so loose that unrelated
    reasons collide.
    """
    if not text:
        return ""
    # Trim to first sentence-y chunk, then first 12 words.
    head = _FM_REASON_TOPIC_SPLIT.split(text, maxsplit=1)[0]
    words = re.sub(r"[^a-zA-Z0-9\s]", " ", head).lower().split()
    return " ".join(words[:12]).strip()


def _parse_fm_reasons(response_text: str) -> list[str]:
    """Best-effort extract of the ``reasons`` list from an FM agent_report.

    The FM agent emits JSON when it works; some older rows are wrapped
    in markdown fences.  Returns ``[]`` on parse failure rather than
    raising — a detector that can't parse a row should not crash the
    report.
    """
    if not response_text:
        return []
    # Strip a leading ```json fence if present.
    txt = response_text.strip()
    if txt.startswith("```"):
        # Drop the first fence + the closing fence if present.
        txt = re.sub(r"^```(?:json)?\s*", "", txt)
        txt = re.sub(r"\s*```\s*$", "", txt)
    try:
        parsed = json.loads(txt)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    reasons = parsed.get("reasons") or []
    out: list[str] = []
    for r in reasons:
        if isinstance(r, str) and r.strip():
            out.append(r.strip())
    return out


def detect_consecutive_fm_rejections_same_theme(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D2 — last N synthesis runs all FM-rejected with topic overlap.

    The signature pattern of the f8faaca bug: the user gives feedback,
    the synthesizer re-runs, the FM rejects on the SAME concern again.
    If ≥3 consecutive completed plan_revision runs were rejected AND
    the normalised topic-set of the latest run overlaps the prior set
    by >50%, the fleet isn't learning — emit RED.

    Uses the most-recent N runs ordered by ``started_at DESC`` to keep
    the comparison fair across long-running synth cycles.
    """
    rows = db.execute(
        select(DecisionRun)
        .where(DecisionRun.user_id == scope.user_id)
        .where(DecisionRun.decision_kind == "plan_revision")
        .where(DecisionRun.status == "completed")
        .order_by(desc(DecisionRun.started_at))
        .limit(5)
    ).scalars().all()
    if len(rows) < 3:
        return []

    # Latest 3 must all be rejected.
    latest_three = rows[:3]
    if not all(r.fund_manager_decision == "rejected" for r in latest_three):
        return []

    # Pull FM reasons for each rejected run.
    topics_per_run: list[tuple[int, set[str]]] = []
    for run in latest_three:
        decision_id_str = f"plan-synth-{run.id}"
        fm = db.execute(
            select(AgentReport)
            .where(AgentReport.user_id == scope.user_id)
            .where(AgentReport.decision_id == decision_id_str)
            .where(AgentReport.agent_role == "fund_manager")
            .order_by(desc(AgentReport.created_at))
            .limit(1)
        ).scalar_one_or_none()
        if fm is None:
            continue
        reasons = _parse_fm_reasons(fm.response_text)
        topics = {_normalise_topic(r) for r in reasons if r}
        topics.discard("")
        topics_per_run.append((run.id, topics))

    if len(topics_per_run) < 3:
        return []

    # Compute pairwise overlap latest-vs-prior and latest-vs-prior-prior.
    latest_topics = topics_per_run[0][1]
    if not latest_topics:
        return []
    overlaps: list[tuple[int, int, float]] = []
    for prior_run_id, prior_topics in topics_per_run[1:]:
        if not prior_topics:
            continue
        intersection = latest_topics & prior_topics
        union = latest_topics | prior_topics
        pct = len(intersection) / len(union) if union else 0.0
        overlaps.append((topics_per_run[0][0], prior_run_id, pct))

    # Trip when the average overlap exceeds 0.5.  Use average so a
    # single noisy round doesn't dominate the signal.
    if not overlaps:
        return []
    avg = sum(o[2] for o in overlaps) / len(overlaps)
    if avg < 0.5:
        return []

    return [Finding(
        id=f"D2:consecutive_rejections:{topics_per_run[0][0]}",
        detector="D2",
        severity="RED",
        category="behavior",
        title=(
            f"3 consecutive plan_revision runs FM-rejected with "
            f"~{avg * 100:.0f}% topic overlap — fleet not learning"
        ),
        evidence={
            "runs": [r.id for r in latest_three],
            "overlap_pcts": [
                {"latest": a, "prior": b, "pct": round(c, 3)}
                for a, b, c in overlaps
            ],
            "latest_topics": sorted(latest_topics),
        },
        suggested_fix=(
            "Check the guidance pipeline (D1 also flags the static "
            "shape).  Verify run_synthesis(guidance=...) actually "
            "threads into Phase 3 + Phase 5 prompts.  Confirm the "
            "user's per-FM-objection stances are being composed and "
            "passed in."
        ),
    )]


# ----------------------------------------------------------------------
# D3 — adapter_outcome_failure_swallowed
# ----------------------------------------------------------------------


def _load_phase_output(db: Session, decision_run_id: int, phase_n: int) -> dict | None:
    """Parse ``phase_output_json`` for one phase as a dict, or None.

    Phase 1's payload is JSON ``{"analyst_reports_text": ..., "adapter_outcomes": [...]}``
    — see ``argosy/orchestrator/flows/plan_synthesis/orchestrator.py:_phase_1_output``.
    Older rows persisted the raw text instead, so a JSON-parse failure
    is benign here.
    """
    row = db.execute(
        select(DecisionPhase)
        .where(DecisionPhase.decision_run_id == decision_run_id)
        .where(DecisionPhase.kind == f"synthesis.phase_{phase_n}")
        .order_by(desc(DecisionPhase.seq))
        .limit(1)
    ).scalar_one_or_none()
    if row is None or not row.phase_output_json:
        return None
    try:
        return json.loads(row.phase_output_json)
    except (json.JSONDecodeError, TypeError):
        return None


def detect_adapter_outcome_failure_swallowed(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D3 — adapter failed but the dependent analyst stayed HIGH confidence.

    For each recent completed plan_revision run:
      1. Read phase_1's adapter_outcomes from phase_output_json.
      2. For each outcome with status in {"http_error", "exception"},
         identify which analyst likely depended on it (by adapter_name
         heuristic — finnhub→news/fundamentals, fred→macro, etc.).
      3. Read the analyst's agent_report.confidence.  If it isn't LOW,
         the analyst is confidently wrong — emit an AMBER finding.

    Adapter→analyst dependency is mapped via a small declarative table
    that's easy to extend.  An adapter we don't know about is logged
    + skipped rather than crashing the detector.
    """
    out: list[Finding] = []
    runs = _recent_completed_runs(db, scope, limit=5)
    if not runs:
        return out

    # Adapter name → analyst agent_role that consumes it.  Heuristic;
    # multiple analysts may consume the same adapter (news/fundamentals
    # both pull finnhub) but the detector is conservative — we map to
    # the PRIMARY consumer.
    ADAPTER_TO_ANALYST = {
        "finnhub": "news",
        "fred": "macro",
        "yfinance": "technical",
        "sec_form4": "fundamentals",
        "sec_13f": "fundamentals",
        "capitoltrades": "fundamentals",
        "tipranks": "sentiment",
        "alphavantage": "fx",
    }

    for run in runs:
        phase_1 = _load_phase_output(db, run.id, 1)
        if not isinstance(phase_1, dict):
            continue
        outcomes = phase_1.get("adapter_outcomes") or []
        if not isinstance(outcomes, list):
            continue
        # Map: analyst_role -> list of failed adapter (name, status, code)
        failed_per_analyst: dict[str, list[dict]] = {}
        for o in outcomes:
            if not isinstance(o, dict):
                continue
            status = o.get("status")
            if status not in ("http_error", "exception"):
                continue
            adapter = o.get("adapter_name") or ""
            analyst = ADAPTER_TO_ANALYST.get(adapter)
            if analyst is None:
                continue
            failed_per_analyst.setdefault(analyst, []).append({
                "adapter": adapter,
                "target": o.get("target"),
                "status": status,
                "http_status": o.get("http_status_code"),
                "error": (o.get("error_text") or "")[:120],
            })

        if not failed_per_analyst:
            continue

        # For each analyst with failed upstreams, look at its confidence.
        decision_id_str = f"plan-synth-{run.id}"
        for analyst_role, failures in failed_per_analyst.items():
            analyst_row = db.execute(
                select(AgentReport)
                .where(AgentReport.user_id == scope.user_id)
                .where(AgentReport.decision_id == decision_id_str)
                .where(AgentReport.agent_role == analyst_role)
                .order_by(desc(AgentReport.created_at))
                .limit(1)
            ).scalar_one_or_none()
            if analyst_row is None:
                continue
            conf = (analyst_row.confidence or "").upper()
            if conf in ("LOW", ""):
                # LOW is the honest signal here; empty is ambiguous (older
                # rows pre-confidence-column) — be conservative and skip.
                continue
            out.append(Finding(
                id=f"D3:{run.id}:{analyst_role}",
                detector="D3",
                severity="AMBER",
                category="data_quality",
                title=(
                    f"`{analyst_role}` analyst returned {conf} "
                    f"confidence despite {len(failures)} failed upstream "
                    f"adapter call(s) (run #{run.id})"
                ),
                evidence={
                    "decision_run_id": run.id,
                    "analyst_role": analyst_role,
                    "analyst_confidence": conf,
                    "failures": failures,
                },
                suggested_fix=(
                    f"Make `{analyst_role}` downgrade its confidence "
                    f"to LOW when its primary adapter returns "
                    f"http_error / exception.  Without this, the "
                    f"synthesizer overweights a hallucinated read."
                ),
            ))
    return out


# ----------------------------------------------------------------------
# D4 — analyst_cites_unknown_source
# ----------------------------------------------------------------------


# Canonical source-id shape in this codebase is
# ``<bucket>/<PROVIDER>/<key>`` where the bucket is lowercase
# (``macro`` / ``news`` / ``portfolio`` / ``fx``), the PROVIDER is
# upper-snake (``FRED`` / ``FINNHUB`` / ``UNKNOWN``), and the key is
# either lower-snake (``oil_wti``) OR upper-snake (``DGS10`` /
# ``DCOILWTICO`` — FRED series ids).  We accept both case shapes in
# the third segment; the validator below compares the literal tokens
# against ``sources_json`` so case sensitivity is preserved.
_SOURCE_ID_RE = re.compile(r"\b([a-z_]+/[A-Z_0-9]+/[A-Za-z0-9_\-]+)\b")


def detect_analyst_cites_unknown_source(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D4 — analyst response_text references a source_id absent from sources_json.

    For each recent analyst row (those carrying a non-empty
    ``sources_json``), scan ``response_text`` for tokens matching the
    canonical source-id shape ``<bucket>/<PROVIDER>/<key>`` (e.g.
    ``macro/FRED/DCOILWTICO``, ``news/FINNHUB/aapl``).  Any cited
    token that doesn't appear in the agent's ``sources_json`` list is
    a hallucinated citation — emit AMBER.
    """
    out: list[Finding] = []
    runs = _recent_completed_runs(db, scope, limit=5)
    if not runs:
        return out

    decision_ids = [f"plan-synth-{r.id}" for r in runs]

    rows = db.execute(
        select(AgentReport)
        .where(AgentReport.user_id == scope.user_id)
        .where(AgentReport.decision_id.in_(decision_ids))
        .where(AgentReport.sources_json.is_not(None))
    ).scalars().all()

    for row in rows:
        if not row.response_text or not row.sources_json:
            continue
        try:
            sources = json.loads(row.sources_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(sources, list):
            continue
        known = set()
        for s in sources:
            if isinstance(s, dict):
                sid = s.get("id") or s.get("source_id") or s.get("kb_id") or ""
            elif isinstance(s, str):
                sid = s
            else:
                sid = ""
            if sid:
                known.add(sid)

        cited = set(_SOURCE_ID_RE.findall(row.response_text))
        # Some cited tokens are KB paths like `docs/design/SDD.md` which
        # don't match the regex above and so won't appear in `cited`.
        # We only flag the strict bucket/PROVIDER/key shape.
        unknown = cited - known
        if not unknown:
            continue
        out.append(Finding(
            id=f"D4:{row.id}",
            detector="D4",
            severity="AMBER",
            category="data_quality",
            title=(
                f"`{row.agent_role}` cited {len(unknown)} source id(s) "
                f"absent from sources_json (agent_report #{row.id})"
            ),
            evidence={
                "agent_report_id": row.id,
                "agent_role": row.agent_role,
                "decision_id": row.decision_id,
                "unknown_sources": sorted(unknown)[:10],
                "known_count": len(known),
            },
            suggested_fix=(
                "Either teach the agent to populate sources_json with "
                "every id it references (preferred), or add a "
                "post-call validator that strips citations the agent "
                "can't back up."
            ),
        ))
    return out


# ----------------------------------------------------------------------
# D5 — empty_payload_but_confident_output
# ----------------------------------------------------------------------


def detect_empty_payload_but_confident_output(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D5 — analyst produced a long response with HIGH confidence despite
    receiving empty upstream payload.

    Today's heuristic: look at the news analyst row.  When phase_1's
    adapter_outcomes show all Finnhub calls returned ``status='empty'``
    (payload_size_bytes==0) AND the news analyst response is >2000
    chars AND confidence is HIGH, we have a confident hallucination.
    Generalises to any analyst→adapter pair by extending the rule
    table below.
    """
    out: list[Finding] = []
    runs = _recent_completed_runs(db, scope, limit=5)
    if not runs:
        return out

    # (analyst_role, adapter_name): primary upstream the analyst leans on.
    RULES = [
        ("news", "finnhub"),
        ("sentiment", "tipranks"),
        ("fundamentals", "sec_13f"),
    ]

    for run in runs:
        phase_1 = _load_phase_output(db, run.id, 1)
        if not isinstance(phase_1, dict):
            continue
        outcomes = phase_1.get("adapter_outcomes") or []
        if not isinstance(outcomes, list):
            continue

        decision_id_str = f"plan-synth-{run.id}"
        for analyst_role, adapter_name in RULES:
            # Count outcomes for this adapter; check all are 'empty'.
            ad_rows = [
                o for o in outcomes
                if isinstance(o, dict) and o.get("adapter_name") == adapter_name
            ]
            if not ad_rows:
                continue
            if not all(o.get("status") == "empty" for o in ad_rows):
                continue
            # Look at the analyst row.
            analyst_row = db.execute(
                select(AgentReport)
                .where(AgentReport.user_id == scope.user_id)
                .where(AgentReport.decision_id == decision_id_str)
                .where(AgentReport.agent_role == analyst_role)
                .order_by(desc(AgentReport.created_at))
                .limit(1)
            ).scalar_one_or_none()
            if analyst_row is None or not analyst_row.response_text:
                continue
            response_chars = len(analyst_row.response_text)
            conf = (analyst_row.confidence or "").upper()
            if conf != "HIGH" or response_chars < 2000:
                continue
            out.append(Finding(
                id=f"D5:{run.id}:{analyst_role}",
                detector="D5",
                severity="AMBER",
                category="data_quality",
                title=(
                    f"`{analyst_role}` returned HIGH confidence + "
                    f"{response_chars} chars despite zero upstream "
                    f"payload from `{adapter_name}` (run #{run.id})"
                ),
                evidence={
                    "decision_run_id": run.id,
                    "analyst_role": analyst_role,
                    "adapter_name": adapter_name,
                    "adapter_outcomes_count": len(ad_rows),
                    "response_chars": response_chars,
                },
                suggested_fix=(
                    f"Add a guard: when `{adapter_name}` returns "
                    f"zero records, `{analyst_role}` should emit "
                    f"`confidence=LOW` and a short 'no data' "
                    f"response.  Long confident output on empty "
                    f"input is the canonical hallucination shape."
                ),
            ))
    return out


# ----------------------------------------------------------------------
# D6 — cost_outlier_per_role
# ----------------------------------------------------------------------


def detect_cost_outlier_per_role(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D6 — an agent_reports row cost more than 3x the median for that role.

    Rolling window: ``scope.lookback_days`` days back (default 14).
    Compares each row's cost against the median of every other row for
    the same agent_role in the same window.  3x median is the trip
    point — generous enough to ignore normal variance, tight enough to
    catch a prompt regression or a runaway model call.

    Skip roles with <5 samples — median is meaningless on tiny n.
    """
    out: list[Finding] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=scope.lookback_days)
    # SQLite returns naive datetimes; pass tz-naive for the comparison.
    cutoff_naive = cutoff.replace(tzinfo=None)

    rows = db.execute(
        select(AgentReport)
        .where(AgentReport.user_id == scope.user_id)
        .where(AgentReport.created_at >= cutoff_naive)
        .where(AgentReport.cost_usd > 0)
    ).scalars().all()
    if not rows:
        return out

    by_role: dict[str, list[AgentReport]] = {}
    for r in rows:
        by_role.setdefault(r.agent_role, []).append(r)

    for role, role_rows in by_role.items():
        if len(role_rows) < 5:
            continue
        costs = [float(r.cost_usd) for r in role_rows]
        median = statistics.median(costs)
        if median <= 0:
            continue
        threshold = 3.0 * median
        for r in role_rows:
            if float(r.cost_usd) <= threshold:
                continue
            out.append(Finding(
                id=f"D6:{r.id}",
                detector="D6",
                severity="YELLOW",
                category="cost",
                title=(
                    f"`{role}` row #{r.id} cost ${float(r.cost_usd):.4f} "
                    f"(>3x median ${median:.4f} over "
                    f"{scope.lookback_days}d, n={len(role_rows)})"
                ),
                evidence={
                    "agent_report_id": r.id,
                    "agent_role": role,
                    "cost_usd": float(r.cost_usd),
                    "median_usd": median,
                    "n_samples": len(role_rows),
                    "tokens_in": r.tokens_in,
                    "tokens_out": r.tokens_out,
                    "decision_id": r.decision_id,
                },
                suggested_fix=(
                    f"Inspect the prompt for `{role}` on this run. "
                    "3x median usually means either an oversized "
                    "context block crept in (prior_items_index "
                    "blew up?) or a retry doubled the call."
                ),
            ))
    return out


# ----------------------------------------------------------------------
# D7 — decision_run_stuck
# ----------------------------------------------------------------------


def detect_decision_run_stuck(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D7 — a ``decision_runs`` row stuck in ``status='running'`` >2h.

    The orphan-sweep in ``argosy.api.main.create_app`` is supposed to
    flip these to 'failed' on startup, but if uvicorn hasn't been
    restarted recently OR the cutoff is wrong, rows accumulate.

    This is RED because a stuck row pollutes the agent-activity view
    (rendered as forever-running on the home page).
    """
    out: list[Finding] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    cutoff_naive = cutoff.replace(tzinfo=None)

    stuck = db.execute(
        select(DecisionRun)
        .where(DecisionRun.user_id == scope.user_id)
        .where(DecisionRun.status == "running")
        .where(DecisionRun.started_at < cutoff_naive)
    ).scalars().all()

    for r in stuck:
        age_h = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - (r.started_at.replace(tzinfo=None) if r.started_at.tzinfo else r.started_at)
        ).total_seconds() / 3600.0
        out.append(Finding(
            id=f"D7:{r.id}",
            detector="D7",
            severity="RED",
            category="reliability",
            title=(
                f"decision_run #{r.id} ({r.decision_kind}) stuck "
                f"running for {age_h:.1f}h"
            ),
            evidence={
                "decision_run_id": r.id,
                "decision_kind": r.decision_kind,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "ticker": r.ticker,
                "age_hours": round(age_h, 2),
            },
            suggested_fix=(
                "Either the orphan-sweep (argosy/api/main.py "
                "_orphan_sweep_at_startup) missed it, or the row is "
                "from a still-active worker — verify in process list "
                "before mass-flipping to 'failed'."
            ),
        ))
    return out


# ----------------------------------------------------------------------
# D8 — phase_participants_empty
# ----------------------------------------------------------------------


def detect_phase_participants_empty(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D8 — a decision_phases row with participants_json=='[]' for a
    synthesis kind.

    T0.1's plumbing is supposed to thread per-phase agent ids into the
    recorder's ``record_negotiation_phase`` so participants_json is
    populated and the /decisions/[id] sequence diagram renders.  An
    empty list means the sub-session persist failed silently OR a
    code path skipped the threading.

    YELLOW because the audit trail is still intact (JSONL trail
    ingest fallback preserves the rows), but the UI features that
    depend on phase_id back-links are degraded.
    """
    out: list[Finding] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=scope.lookback_days)
    cutoff_naive = cutoff.replace(tzinfo=None)

    rows = db.execute(
        select(DecisionPhase)
        .join(DecisionRun, DecisionPhase.decision_run_id == DecisionRun.id)
        .where(DecisionRun.user_id == scope.user_id)
        .where(DecisionPhase.created_at >= cutoff_naive)
        .where(DecisionPhase.kind.like("synthesis.%"))
        .where(DecisionPhase.participants_json == "[]")
    ).scalars().all()
    if not rows:
        return out

    # Group by decision_run_id to avoid emitting one finding per phase
    # — surface the run-level pattern instead.
    by_run: dict[int, list[DecisionPhase]] = {}
    for r in rows:
        by_run.setdefault(r.decision_run_id, []).append(r)

    for run_id, phases in sorted(by_run.items(), reverse=True):
        out.append(Finding(
            id=f"D8:{run_id}",
            detector="D8",
            severity="YELLOW",
            category="reliability",
            title=(
                f"decision_run #{run_id} has {len(phases)} synthesis "
                f"phase row(s) with empty participants_json"
            ),
            evidence={
                "decision_run_id": run_id,
                "phase_kinds_empty": sorted({p.kind for p in phases}),
                "phase_count": len(phases),
            },
            suggested_fix=(
                "Trace _persist_phase_agent_reports_async + "
                "_record_phase_completion in plan_synthesis/"
                "orchestrator.py — a failure there silently writes "
                "participants_json='[]' and the UI sequence diagram "
                "comes up empty."
            ),
        ))
    return out


# ----------------------------------------------------------------------
# D9 — objection_topic_recurrence
# ----------------------------------------------------------------------


def detect_objection_topic_recurrence(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D9 — one FM objection topic recurs across N≥3 consecutive runs.

    Sister of D2 (which checks aggregate overlap); D9 surfaces SPECIFIC
    topics that the fleet can't close.  Useful as a per-topic
    actionable list ("NVDA share-count arithmetic" keeps showing up —
    fix the analyst).

    The normalisation is intentionally generous (first 12 words,
    lowercased) so synonyms don't fragment the count.  When precision
    matters, the evidence block includes the raw reason strings.
    """
    out: list[Finding] = []
    runs = _recent_completed_runs(db, scope, limit=6)
    if len(runs) < 3:
        return out

    # Pull FM reasons per run keyed by normalised topic; preserve raw.
    topic_to_runs: dict[str, list[tuple[int, str]]] = {}
    for run in runs:
        if run.fund_manager_decision != "rejected":
            continue
        decision_id_str = f"plan-synth-{run.id}"
        fm = db.execute(
            select(AgentReport)
            .where(AgentReport.user_id == scope.user_id)
            .where(AgentReport.decision_id == decision_id_str)
            .where(AgentReport.agent_role == "fund_manager")
            .order_by(desc(AgentReport.created_at))
            .limit(1)
        ).scalar_one_or_none()
        if fm is None:
            continue
        for raw in _parse_fm_reasons(fm.response_text):
            t = _normalise_topic(raw)
            if not t:
                continue
            topic_to_runs.setdefault(t, []).append((run.id, raw[:160]))

    for topic, runs_with_text in topic_to_runs.items():
        run_ids = {r[0] for r in runs_with_text}
        if len(run_ids) < 3:
            continue
        out.append(Finding(
            id=f"D9:{topic[:50].replace(' ', '_')}",
            detector="D9",
            severity="AMBER",
            category="behavior",
            title=(
                f"FM objection topic '{topic[:50]}...' recurs in "
                f"{len(run_ids)} runs — fleet can't close it"
            ),
            evidence={
                "topic": topic,
                "run_ids": sorted(run_ids, reverse=True),
                "sample_reasons": [r[1] for r in runs_with_text[:3]],
            },
            suggested_fix=(
                "Either the upstream analyst data is wrong (fix that "
                "first) or the synthesizer prompt isn't internalising "
                "the FM's prior rejection (escalate to the "
                "guidance pipeline)."
            ),
        ))
    return out


# ----------------------------------------------------------------------
# D10 — agent_response_truncated
# ----------------------------------------------------------------------

_TRAILING_OK_PUNCT = {".", "!", "?", "}", "]", ")", "\"", "'"}


def detect_agent_response_truncated(
    db: Session, scope: ReviewScope,
) -> list[Finding]:
    """D10 — response_text ends mid-stream (likely hit max_tokens).

    Heuristic: ``response_text`` ends with a non-terminal character OR
    contains an obvious mid-stream cut (e.g. ``", "reasons": [`` at
    the end).  We trim trailing whitespace first.
    """
    out: list[Finding] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=scope.lookback_days)
    cutoff_naive = cutoff.replace(tzinfo=None)

    rows = db.execute(
        select(AgentReport)
        .where(AgentReport.user_id == scope.user_id)
        .where(AgentReport.created_at >= cutoff_naive)
        .where(func.length(AgentReport.response_text) > 200)
    ).scalars().all()
    for r in rows:
        text = (r.response_text or "").rstrip()
        if not text:
            continue
        last = text[-1]
        if last in _TRAILING_OK_PUNCT:
            continue
        # Tail looks mid-stream: ends with a quoted key, an opening
        # bracket, a comma, etc.  Don't flag rows that end with a
        # word (free-form text agents often do).
        if last in (",", ":", "[", "{", "\"", "'"):
            out.append(Finding(
                id=f"D10:{r.id}",
                detector="D10",
                severity="AMBER",
                category="reliability",
                title=(
                    f"`{r.agent_role}` agent_report #{r.id} "
                    f"response appears truncated (ends with '{last}')"
                ),
                evidence={
                    "agent_report_id": r.id,
                    "agent_role": r.agent_role,
                    "decision_id": r.decision_id,
                    "response_chars": len(r.response_text or ""),
                    "tail_excerpt": text[-80:],
                    "tokens_out": r.tokens_out,
                },
                suggested_fix=(
                    "Either bump the agent's `max_tokens` (the prompt "
                    "is currently emitting more than the cap permits) "
                    "or shorten the schema so the model finishes."
                ),
            ))
    return out


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------


def _recent_completed_runs(
    db: Session, scope: ReviewScope, *, limit: int = 5,
) -> list[DecisionRun]:
    """Latest N COMPLETED plan_revision runs for the user, newest first."""
    return list(db.execute(
        select(DecisionRun)
        .where(DecisionRun.user_id == scope.user_id)
        .where(DecisionRun.decision_kind == "plan_revision")
        .where(DecisionRun.status == "completed")
        .order_by(desc(DecisionRun.started_at))
        .limit(limit)
    ).scalars().all())


# ----------------------------------------------------------------------
# Registry — extend by appending a new (id, name, fn) tuple.
# ----------------------------------------------------------------------


ALL_DETECTORS: tuple[tuple[str, str, callable], ...] = (
    ("D1", "guidance_pipeline_no_op", detect_guidance_pipeline_no_op),
    ("D2", "consecutive_fm_rejections_same_theme",
     detect_consecutive_fm_rejections_same_theme),
    ("D3", "adapter_outcome_failure_swallowed",
     detect_adapter_outcome_failure_swallowed),
    ("D4", "analyst_cites_unknown_source", detect_analyst_cites_unknown_source),
    ("D5", "empty_payload_but_confident_output",
     detect_empty_payload_but_confident_output),
    ("D6", "cost_outlier_per_role", detect_cost_outlier_per_role),
    ("D7", "decision_run_stuck", detect_decision_run_stuck),
    ("D8", "phase_participants_empty", detect_phase_participants_empty),
    ("D9", "objection_topic_recurrence", detect_objection_topic_recurrence),
    ("D10", "agent_response_truncated", detect_agent_response_truncated),
)


def run_all_detectors(
    db: Session, scope: ReviewScope,
) -> tuple[list[Finding], list[dict]]:
    """Run every detector. Returns (findings, per-detector stats).

    Each detector body is wrapped in try/except — a detector that
    crashes (schema mismatch, missing column, JSON parse error not
    caught internally) logs + skips with an empty result rather than
    aborting the entire report.  The runner relies on this contract
    to keep producing reports even when one detector breaks.

    Returns:
      (findings, stats) where stats is a list of dicts like
      ``{"detector": "D1", "name": "...", "ok": True, "count": 0,
      "error": None}``.  The runner surfaces stats in the report's
      diagnostic footer so the user can see WHICH detector failed
      vs. which simply found nothing.
    """
    findings: list[Finding] = []
    stats: list[dict] = []
    for det_id, name, fn in ALL_DETECTORS:
        try:
            results = fn(db, scope)
        except Exception as exc:  # noqa: BLE001 — defensive: never crash report
            log.warning(
                "fleet_self_review.detector_failed",
                detector=det_id, name=name, error=str(exc),
            )
            stats.append({
                "detector": det_id, "name": name, "ok": False,
                "count": 0, "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        # Defensive: ensure each result is a Finding (a detector that
        # returns a stray dict shouldn't poison downstream JSON).
        clean = [f for f in results if isinstance(f, Finding)]
        findings.extend(clean)
        stats.append({
            "detector": det_id, "name": name, "ok": True,
            "count": len(clean), "error": None,
        })
    return findings, stats


def finding_to_dict(f: Finding) -> dict:
    """JSON-serialisable form of a Finding."""
    return asdict(f)


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    """``{"RED": N, "AMBER": M, "YELLOW": K}`` summary for the badge."""
    out = {"RED": 0, "AMBER": 0, "YELLOW": 0}
    for f in findings:
        if f.severity in out:
            out[f.severity] += 1
    return out


__all__ = [
    "ALL_DETECTORS",
    "Finding",
    "ReviewScope",
    "Severity",
    "detect_adapter_outcome_failure_swallowed",
    "detect_agent_response_truncated",
    "detect_analyst_cites_unknown_source",
    "detect_consecutive_fm_rejections_same_theme",
    "detect_cost_outlier_per_role",
    "detect_decision_run_stuck",
    "detect_empty_payload_but_confident_output",
    "detect_guidance_pipeline_no_op",
    "detect_objection_topic_recurrence",
    "detect_phase_participants_empty",
    "finding_to_dict",
    "run_all_detectors",
    "severity_counts",
]
