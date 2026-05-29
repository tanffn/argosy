"""State-observer backfill verifier (Spec B commit #5 — empirical merge gate).

Per ``docs/superpowers/specs/2026-05-29-state-observer-agent-design.md`` §5 +
Appendix C: the empirical contract of the state-observer architecture is
that the historical USD/NIS 3.6 -> 2.8 case surfaces as an EMERGENT flag
without "FX" being hardcoded anywhere in the prompt or the code. This
script proves it.

The flow
========

1. Discover (or accept) K snapshot dates spanning the user's plan window.
2. For each date, reconstruct the historical state snapshot via
   :func:`argosy.services.state_snapshot.collect_state_snapshot` with
   ``as_of=<date>``.
3. Build the vs-plan and vs-prior diffs via
   :func:`argosy.services.state_diff.compute_full_diff`.
4. Call :class:`argosy.agents.state_observer.StateObserverAgent.run` once
   per snapshot (default) or K times per snapshot (with ``--k-samples``)
   to enable the Appendix C K=5/M=4 probabilistic gate.
5. Record per-snapshot flag candidates + write a verification report.
6. Assert the acceptance gates (§5 / Appendix C.2):

   * **Merge gate**: the most-recent snapshot MUST surface an FX flag
     (``primary_field`` matches ``macro.fx_*``) at severity
     ``warning`` or ``critical`` in M=4 of K=5 samples (default). The
     gate is the empirical proof that the architecture catches what
     the hand-rolled detectors missed.

   * **Hit-rate gate**: across the K-by-N samples for the most-recent
     date, at least M of K must surface the FX flag. Configurable via
     ``--m-fx-required`` (defaults to 4).

   * **Noise SLO** (logged, not gating below median=5): each snapshot
     SHOULD median <= 3 flags. Above 5 fails.

7. Exit 0 when all gates pass; exit 1 (with the report dumped to
   stdout) when any gate fails. CI catches this.

Modes
=====

``--dry-run`` (default for tests): no Opus calls. The script
instantiates ``_FakeStateObserverAgent``, which reads canned candidate
lists from a fixture file. This lets the script's report logic +
gate arithmetic be tested without spending tokens. The fixture lives
at ``tests/fixtures/state_observer_backfill_smoke/fx_3p6_to_2p8_acceptance.json``
by default; override with ``--fixture <path>``.

``--real-llm``: the live empirical gate. Spends real Opus 4.7 calls
(K samples per snapshot, default K=5 -> up to 25 calls for a 5-snapshot
backfill). Per [[feedback_accuracy_over_cost]] this is the
binding-tolerant mode for manual verification before merge. **No
test is marked to exercise this
mode** -- the test command (`pytest -m "not llm_eval"`) skips
`llm_eval` and we deliberately do not tag the dry-run tests with it.

CLI flags
=========

  ``--user-id``         tenant (default ``ariel``).
  ``--snapshots``       number of historical anchors to walk (default 5;
                        matches Appendix C K-samples conceptually but
                        controls N here, not K).
  ``--as-of-dates``     comma-separated ISO dates that OVERRIDE the
                        auto-discovery walk (e.g.
                        ``2026-05-29,2026-04-29,2026-03-30``). Last
                        date listed is treated as the most-recent for
                        the merge gate.
  ``--interval-days``   spacing between auto-discovered dates
                        (default 30).
  ``--k-samples``       samples per snapshot (default 5 -- Appendix C
                        K=5 contract).
  ``--m-fx-required``   FX flag must surface in this many of K samples
                        on the most-recent snapshot (default 4 --
                        Appendix C M=4 contract).
  ``--dry-run``         use the fake agent + fixture; default
                        ``True`` (off only via ``--real-llm``).
  ``--real-llm``        opposite of ``--dry-run``; opt-in to actual
                        Opus calls.
  ``--fixture <path>``  override the dry-run fixture path.
  ``--report-out``      write the JSON report to this file in addition
                        to printing it; defaults to stdout only.

Degraded-mode behavior
======================

When the user's local DB has FEWER snapshots than ``--snapshots``
requests, the script logs a WARNING for each missing date and
continues with whatever can be reconstructed. The acceptance gates
still run; if the most-recent snapshot can't be reconstructed the
merge gate fails LOUDLY (exit 1 + report). Per CLAUDE.md, the dev DB
currently has one portfolio snapshot at 2026-03-24 -- the historical
replay anchors past that gracefully degrade rather than silently
filling with today's values (see state_snapshot.StateReplayError).

Importable for tests
====================

The acceptance gates + report assembly + the fake agent are exposed
as top-level callables so the test suite (`tests/test_state_observer_backfill.py`)
can exercise them without invoking ``main()``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants — Appendix C defaults
# ---------------------------------------------------------------------------

#: Per Appendix C K-samples acceptance gate.
DEFAULT_K_SAMPLES: int = 5

#: Per Appendix C M-of-K contract for the FX-merge gate.
DEFAULT_M_FX_REQUIRED: int = 4

#: Per spec §5.2: walk N monthly snapshots back from today.
DEFAULT_N_SNAPSHOTS: int = 5

#: Per spec §5.2: interval between auto-discovered dates.
DEFAULT_INTERVAL_DAYS: int = 30

#: Per spec §5.3 acceptance: severity 'warning' or 'critical' counts as
#: "surfaced". 'info' on the most-recent snapshot does NOT count.
ACCEPTABLE_FX_SEVERITIES: frozenset[str] = frozenset({"warning", "critical"})

#: The PRIMARY FX field for the merge gate (codex 2026-05-29 BLOCKER #1).
#: The merge gate's empirical contract is specifically the spot-rate
#: deviation (3.6 plan -> 2.8 current). Looser matchers like ``macro.fx_*``
#: would false-pass when the observer flagged only ``macro.fx_usd_nis_30d_avg``
#: (a derived statistic) but missed the spot. Counting requires the
#: exact spot field OR an explicitly-allowlisted alias.
FX_MERGE_GATE_PRIMARY_FIELDS: frozenset[str] = frozenset({
    "macro.fx_usd_nis_spot",
})

#: Severity rank for ordering "max severity across candidates"
#: comparisons (codex 2026-05-29 BLOCKER #2). Higher is more severe.
_SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}

#: Noise SLO -- median flag count per snapshot. Per Appendix C.2 NICE #3:
#: median > 3 is a logged SLO miss, median > 5 fails the gate.
NOISE_MEDIAN_SLO: int = 3
NOISE_MEDIAN_HARD_FAIL: int = 5

#: Default fixture path for ``--dry-run`` (relative to repo root).
DEFAULT_FIXTURE_PATH = (
    "tests/fixtures/state_observer_backfill_smoke/fx_3p6_to_2p8_acceptance.json"
)

_log = logging.getLogger("argosy.scripts.state_observer_backfill")


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


@dataclass
class SnapshotResult:
    """Per-snapshot record stamped into the report.

    Attributes:
      snapshot_date: ISO date (or 'unavailable' when collection failed).
      sample_idx:    0..K-1 within the snapshot.
      flag_candidates_count: total flags emitted by the observer.
      fx_flag_surfaced: True iff at least one ``primary_field`` matches
        ``macro.fx_*``. The merge gate's structural test.
      fx_flag_severity: severity of the (first matching) FX flag, or
        None when none surfaced. The acceptance gate's severity check.
      overall_assessment: the LLM's gestalt summary.
      confidence: the output-level confidence.
      degraded_reason: filled with a short string when the snapshot
        couldn't be reconstructed (e.g. ``"StateReplayError: no plan"``);
        empty otherwise.
    """

    snapshot_date: str
    sample_idx: int
    flag_candidates_count: int = 0
    fx_flag_surfaced: bool = False
    fx_flag_severity: str | None = None
    overall_assessment: str = ""
    confidence: str | None = None
    degraded_reason: str = ""


@dataclass
class BackfillReport:
    """Full backfill report -- the artefact CI inspects.

    Attributes:
      user_id:           tenant.
      generated_at:      ISO timestamp of report assembly.
      mode:              ``"dry-run"`` or ``"real-llm"``.
      k_samples:         K from Appendix C.
      m_fx_required:     M from Appendix C.
      snapshot_dates:    the dates the script attempted, ISO-sorted ASC.
      most_recent_date:  the merge-gate anchor (max(snapshot_dates)).
      results:           per-(date, sample) flat list of SnapshotResult.
      gate_outcomes:     per-gate True/False with diagnostic text.
      degraded_dates:    dates whose snapshot couldn't be reconstructed.
    """

    user_id: str
    generated_at: str
    mode: str
    k_samples: int
    m_fx_required: int
    snapshot_dates: list[str] = field(default_factory=list)
    most_recent_date: str = ""
    results: list[SnapshotResult] = field(default_factory=list)
    gate_outcomes: dict[str, Any] = field(default_factory=dict)
    degraded_dates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "k_samples": self.k_samples,
            "m_fx_required": self.m_fx_required,
            "snapshot_dates": self.snapshot_dates,
            "most_recent_date": self.most_recent_date,
            "results": [asdict(r) for r in self.results],
            "gate_outcomes": self.gate_outcomes,
            "degraded_dates": self.degraded_dates,
        }


# ---------------------------------------------------------------------------
# Fixture-driven fake agent (dry-run path)
# ---------------------------------------------------------------------------


class _FakeStateObserverAgent:
    """Deterministic stand-in for :class:`StateObserverAgent`.

    Reads canned candidate-lists from the fixture and dispenses them
    keyed by (snapshot_date, sample_idx). NEVER touches Opus. The
    dispenser is the source of truth for the dry-run path's output --
    once you understand the fixture, you understand exactly what
    every dry-run will emit.

    Designed to be **byte-identical** across invocations: no time
    sources, no RNG, no env reads. The fixture file IS the contract.

    Public API mirrors the subset of ``StateObserverAgent`` the
    backfill script depends on:

    * ``__init__(user_id)``        -- mirrors the real agent ctor.
    * ``async run(**inputs)``       -- returns an object with
      ``.output`` of shape ``StateObserverOutput``.

    The ``run`` kwargs are READ but mostly IGNORED: the fake agent
    pulls a sample based on ``inputs['snapshot_date']`` and the
    per-instance ``sample_idx_counter``. This is exactly the shape
    the real agent expects so production callers can substitute the
    fake without changing their wiring.
    """

    def __init__(
        self,
        *,
        user_id: str,
        fixture: dict[str, Any],
    ) -> None:
        self.user_id = user_id
        self.agent_role = "state_observer"
        self.model = "fake-fixture-agent"
        # The fake agent serves samples by (snapshot_date, idx). The
        # idx is per-date; bumped per call. A test can reset it between
        # passes by re-instantiating the agent.
        self._fixture = fixture
        self._idx_by_date: dict[str, int] = {}
        self.call_count = 0

    # The script's call signature uses kwargs only; we don't need
    # to support build_prompt / _call_model / _parse_output paths.
    async def run(self, **inputs: Any) -> Any:
        from argosy.agents.base import AgentReport, ConfidenceBand
        from argosy.agents.state_observer import (
            FlagCandidate,
            StateObserverOutput,
        )

        self.call_count += 1
        # Date may arrive as a `date`, `datetime`, or ISO str depending
        # on the caller; normalise to ISO.
        snap_date = inputs.get("snapshot_date") or inputs.get("as_of")
        if isinstance(snap_date, (date, datetime)):
            snap_date_iso = snap_date.isoformat()[:10]
        else:
            snap_date_iso = str(snap_date or "")[:10]

        idx = self._idx_by_date.get(snap_date_iso, 0)
        self._idx_by_date[snap_date_iso] = idx + 1

        sample_payload = self._lookup_sample(snap_date_iso, idx)
        if sample_payload is None:
            # Graceful degradation: no fixture data for this date.
            # Emit an empty output rather than raising.
            empty = StateObserverOutput(
                flag_candidates=[],
                overall_assessment=(
                    f"(no fixture data for snapshot_date={snap_date_iso!r}; "
                    "fake agent emitted empty output)"
                ),
                confidence=ConfidenceBand.LOW,
                cited_sources=[],
            )
            return _wrap_in_report(empty, agent=self)

        # Reconstruct the StateObserverOutput from the canned dict.
        candidates: list[FlagCandidate] = []
        for raw in sample_payload.get("flag_candidates") or []:
            try:
                candidates.append(FlagCandidate(**raw))
            except Exception as exc:  # noqa: BLE001 -- defensive
                _log.warning(
                    "fake_agent.skipping_invalid_candidate: %s (payload=%r)",
                    exc, raw,
                )
                continue

        conf_raw = sample_payload.get("confidence", "MEDIUM")
        try:
            conf = ConfidenceBand(conf_raw) if isinstance(conf_raw, str) else conf_raw
        except ValueError:
            conf = ConfidenceBand.MEDIUM

        output = StateObserverOutput(
            flag_candidates=candidates,
            overall_assessment=sample_payload.get("overall_assessment", ""),
            confidence=conf,
            cited_sources=list(sample_payload.get("cited_sources") or []),
        )
        return _wrap_in_report(output, agent=self)

    def _lookup_sample(
        self, snapshot_date_iso: str, sample_idx: int,
    ) -> dict[str, Any] | None:
        """Find ``snapshots[i].samples[sample_idx]`` by snapshot_date."""
        for snap in self._fixture.get("snapshots", []):
            if snap.get("snapshot_date") == snapshot_date_iso:
                samples = snap.get("samples") or []
                if 0 <= sample_idx < len(samples):
                    return samples[sample_idx]
                # Sample index out of bounds (caller asked for more K
                # than the fixture has). Return None -> empty output.
                return None
        return None


def _wrap_in_report(output: Any, *, agent: Any) -> Any:
    """Wrap an output in a minimal ``AgentReport``-shaped object.

    The script reads ``report.output.flag_candidates`` /
    ``report.output.confidence`` etc., so the dataclass needs to
    expose ``.output``. Tokens / cost / model are zero / empty for
    the fake; they're not asserted on by the backfill gates.
    """
    from argosy.agents.base import AgentReport

    return AgentReport(
        agent_role=agent.agent_role,
        user_id=agent.user_id,
        model=agent.model,
        response_text="",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        prompt_hash="",
        confidence=output.confidence,
        output=output,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def load_fixture(path: str | Path) -> dict[str, Any]:
    """Load + validate the dry-run fixture.

    Raises ``FileNotFoundError`` when the path doesn't exist (so a
    misconfigured CI run fails LOUDLY rather than silently passing
    on an empty fixture). Raises ``ValueError`` when the JSON is
    structurally invalid (missing 'snapshots' key, or 'snapshots' is
    not a list).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"state_observer_backfill: fixture not found at {p}. "
            f"Pass --fixture <path> to override, or run with --real-llm "
            f"to skip the dry-run path."
        )
    with p.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "snapshots" not in data:
        raise ValueError(
            f"state_observer_backfill: fixture at {p} missing top-level "
            f"'snapshots' key; got top-level keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        )
    if not isinstance(data["snapshots"], list):
        raise ValueError(
            f"state_observer_backfill: fixture at {p} 'snapshots' must "
            f"be a list; got {type(data['snapshots']).__name__}"
        )
    return data


# ---------------------------------------------------------------------------
# Date discovery
# ---------------------------------------------------------------------------


def discover_snapshot_dates(
    *,
    n_snapshots: int,
    interval_days: int,
    explicit_dates: list[str] | None = None,
    anchor_today: date | None = None,
) -> list[date]:
    """Build the ordered (ASC) list of snapshot dates the backfill walks.

    Args:
      n_snapshots: how many monthly anchors to discover.
      interval_days: spacing between auto-discovered anchors.
      explicit_dates: when provided, OVERRIDES auto-discovery. Parsed
        as ISO; sorted ASC for the report; the LAST date is the
        merge-gate anchor.
      anchor_today: override ``today``; used by tests for determinism.

    Returns:
      List of ``date`` objects, ASC. Always non-empty (if both
      explicit_dates is None/empty AND n_snapshots <= 0, raises).

    Raises:
      ValueError: when both inputs degenerate to empty.
    """
    if explicit_dates:
        out: list[date] = []
        for s in explicit_dates:
            s = s.strip()
            if not s:
                continue
            try:
                out.append(date.fromisoformat(s))
            except ValueError as exc:
                raise ValueError(
                    f"state_observer_backfill: invalid ISO date in "
                    f"--as-of-dates: {s!r} ({exc})"
                ) from exc
        if not out:
            raise ValueError(
                "state_observer_backfill: --as-of-dates was set but "
                "no valid ISO dates parsed."
            )
        # Codex IMPORTANT #2 (2026-05-29 review): detect duplicates
        # rather than silently de-duping. Operator typos that pass the
        # same date twice should be loud — masking them risks running
        # the merge gate against fewer effective anchors than intended.
        seen: dict[date, int] = {}
        duplicates: list[str] = []
        for d in out:
            seen[d] = seen.get(d, 0) + 1
        for d, n in seen.items():
            if n > 1:
                duplicates.append(f"{d.isoformat()} (x{n})")
        if duplicates:
            raise ValueError(
                "state_observer_backfill: duplicate dates in "
                f"--as-of-dates: {', '.join(sorted(duplicates))}. Remove "
                "the duplicates to make the K-by-N walk unambiguous."
            )
        # Sort ASC for the merge-gate anchor selection.
        return sorted(out)

    if n_snapshots <= 0:
        raise ValueError(
            "state_observer_backfill: --snapshots must be >= 1 (got "
            f"{n_snapshots})"
        )
    anchor = anchor_today or date.today()
    # We walk BACKWARDS from anchor in interval_days steps, then ASC-sort.
    dates_ = [anchor - timedelta(days=i * interval_days) for i in range(n_snapshots)]
    return sorted(set(dates_))


# ---------------------------------------------------------------------------
# Per-snapshot runner
# ---------------------------------------------------------------------------


async def run_observer_on_snapshot(
    *,
    agent: Any,
    snapshot_date_iso: str,
    sample_idx: int,
    state_inputs: dict[str, Any],
) -> SnapshotResult:
    """Invoke the observer for one (date, sample) cell and pack the result.

    ``state_inputs`` is the kwargs dict that goes into ``agent.run(**state_inputs)``.
    For the dry-run path we mostly only need ``snapshot_date``; the
    real-LLM path needs the full Appendix B build_prompt inputs.

    The returned SnapshotResult is shape-stable across dry-run and
    real-LLM modes so the gate logic doesn't care which path produced
    the row.
    """
    try:
        report = await agent.run(**state_inputs)
    except Exception as exc:  # noqa: BLE001 -- defensive
        # A single bad LLM run shouldn't take down the backfill --
        # we record it as a degraded cell and let the gates judge.
        _log.warning(
            "state_observer_backfill.agent_run_failed",
            extra={
                "snapshot_date": snapshot_date_iso,
                "sample_idx": sample_idx,
                "error": str(exc)[:200],
            },
        )
        return SnapshotResult(
            snapshot_date=snapshot_date_iso,
            sample_idx=sample_idx,
            flag_candidates_count=0,
            fx_flag_surfaced=False,
            fx_flag_severity=None,
            overall_assessment="",
            confidence=None,
            degraded_reason=f"agent_run_failed: {type(exc).__name__}: {str(exc)[:120]}",
        )

    output = report.output
    candidates = list(getattr(output, "flag_candidates", []) or [])

    # Codex BLOCKER #1: the merge gate requires an EXACT match on the
    # spot field (or an explicitly-allowlisted alias). Counting a
    # candidate whose primary_field is `macro.fx_usd_nis_30d_avg`
    # would false-pass the 3.6 -> 2.8 spot proof.
    #
    # Codex BLOCKER #2: when MULTIPLE candidates match (e.g. the LLM
    # emits both a critical FX flag AND an info FX flag — unlikely but
    # not impossible), the severity recorded for the cell is the MAX
    # severity across matches, not the first-seen. The first-seen
    # ordering depended on whatever order the LLM emitted candidates
    # in, which the gate cannot rely on.
    fx_surfaced = False
    fx_severity: str | None = None
    fx_max_rank = -1
    for cand in candidates:
        primary = getattr(cand, "primary_field", "")
        if not isinstance(primary, str):
            continue
        if primary not in FX_MERGE_GATE_PRIMARY_FIELDS:
            continue
        sev = getattr(cand, "severity", None)
        sev_str = str(sev) if sev is not None else None
        rank = _SEVERITY_RANK.get(sev_str or "", -1)
        if rank > fx_max_rank:
            fx_max_rank = rank
            fx_severity = sev_str
            fx_surfaced = True

    conf = getattr(output, "confidence", None)
    conf_val = None
    if conf is not None:
        # ConfidenceBand is an Enum; pull the .value.
        conf_val = getattr(conf, "value", str(conf))

    return SnapshotResult(
        snapshot_date=snapshot_date_iso,
        sample_idx=sample_idx,
        flag_candidates_count=len(candidates),
        fx_flag_surfaced=fx_surfaced,
        fx_flag_severity=fx_severity,
        overall_assessment=str(getattr(output, "overall_assessment", "") or ""),
        confidence=conf_val,
    )


# ---------------------------------------------------------------------------
# Gate logic — Appendix C.2
# ---------------------------------------------------------------------------


def evaluate_acceptance_gates(
    report: BackfillReport,
) -> dict[str, Any]:
    """Evaluate Appendix C.2 gates against the recorded results.

    Returns a dict packed into ``report.gate_outcomes``. The same dict
    is the gate evaluation any caller can consume (test or CLI).

    Gate keys:
      ``merge_gate_fx_on_most_recent`` -- bool: most recent date had
        >= M_FX_REQUIRED samples with an FX flag at warning|critical.
      ``merge_gate_detail`` -- diagnostic text describing pass/fail.
      ``severity_band_ok_on_most_recent`` -- bool: of the FX surfaces
        on the most-recent date, all were warning|critical (not info).
      ``noise_slo`` -- per-date dict: ``{date: {"median": N, "samples":
        [N, ...], "status": "ok"|"slo_miss"|"hard_fail"}}``.
      ``severity_does_not_invert`` -- bool: median FX severity by
        date is non-decreasing as the deviation grew.
      ``severity_band_detail`` -- diagnostic per-date severity medians.
      ``all_gates_passed`` -- bool: True iff every gate above passed.
    """
    outcomes: dict[str, Any] = {}

    # Group results by date for the per-snapshot gates.
    by_date: dict[str, list[SnapshotResult]] = {}
    for r in report.results:
        by_date.setdefault(r.snapshot_date, []).append(r)

    sorted_dates = sorted(by_date.keys())  # ISO ascending
    most_recent = report.most_recent_date

    # --- merge gate: M of K FX surfaces on the most recent date,
    #     with severity in {warning, critical}.
    most_recent_results = by_date.get(most_recent, [])
    fx_at_acceptable_severity = [
        r for r in most_recent_results
        if r.fx_flag_surfaced and r.fx_flag_severity in ACCEPTABLE_FX_SEVERITIES
    ]
    n_surfaces_acceptable = len(fx_at_acceptable_severity)
    k_observed = len(most_recent_results)

    merge_passed = n_surfaces_acceptable >= report.m_fx_required
    outcomes["merge_gate_fx_on_most_recent"] = merge_passed
    outcomes["merge_gate_detail"] = (
        f"on most_recent_date={most_recent!r}: "
        f"{n_surfaces_acceptable}/{k_observed} samples surfaced an FX flag "
        f"at severity in {sorted(ACCEPTABLE_FX_SEVERITIES)}. "
        f"Required: M={report.m_fx_required} of K={report.k_samples}. "
        f"{'PASS' if merge_passed else 'FAIL'}."
    )

    # --- severity-band check: of the K samples on the most-recent
    #     snapshot, we don't want any FX flag at 'info' (that would
    #     mean the LLM judged the 22% deviation as "not material").
    #     We DO allow no FX flag at all in some samples -- the merge
    #     gate's M-of-K already accommodates that. This check is: when
    #     FX surfaces, severity should be acceptable.
    info_surfaces = [
        r for r in most_recent_results
        if r.fx_flag_surfaced and r.fx_flag_severity == "info"
    ]
    severity_band_ok = len(info_surfaces) == 0
    outcomes["severity_band_ok_on_most_recent"] = severity_band_ok
    outcomes["severity_band_detail"] = (
        f"info-severity FX surfaces on most_recent: {len(info_surfaces)} "
        f"(expected 0). On the 22% USD/NIS deviation, info is too soft."
    )

    # --- noise SLO per snapshot.
    noise_outcomes: dict[str, Any] = {}
    noise_hard_fail = False
    for d in sorted_dates:
        counts = [r.flag_candidates_count for r in by_date[d]]
        if not counts:
            noise_outcomes[d] = {"median": None, "samples": [], "status": "no_data"}
            continue
        med = statistics.median(counts)
        if med > NOISE_MEDIAN_HARD_FAIL:
            status = "hard_fail"
            noise_hard_fail = True
        elif med > NOISE_MEDIAN_SLO:
            status = "slo_miss"
        else:
            status = "ok"
        noise_outcomes[d] = {"median": med, "samples": counts, "status": status}
    outcomes["noise_slo"] = noise_outcomes
    outcomes["noise_hard_fail"] = noise_hard_fail

    # --- severity-non-decreasing across the time window. We compute
    #     median FX severity rank per date (info=0/warning=1/critical=2);
    #     each pair adjacent in time should NOT have current<prior-0.5.
    severity_rank = {"info": 0, "warning": 1, "critical": 2}
    median_sev_by_date: dict[str, float | None] = {}
    for d in sorted_dates:
        sev_ranks = [
            severity_rank.get(r.fx_flag_severity, None)
            for r in by_date[d] if r.fx_flag_surfaced and r.fx_flag_severity in severity_rank
        ]
        if sev_ranks:
            median_sev_by_date[d] = float(statistics.median(sev_ranks))
        else:
            median_sev_by_date[d] = None

    sev_inverted = False
    inversion_detail: list[str] = []
    prev_date: str | None = None
    prev_sev: float | None = None
    for d in sorted_dates:
        cur_sev = median_sev_by_date[d]
        if cur_sev is None:
            # Skip the comparison anchor when the current date has no
            # FX surfaces (no signal to compare against). The next
            # date's comparison will continue from the LAST non-None
            # anchor we saw.
            continue
        if prev_sev is not None:
            if cur_sev < prev_sev - 0.5:
                sev_inverted = True
                inversion_detail.append(
                    f"between {prev_date} (median rank {prev_sev}) and "
                    f"{d} (median rank {cur_sev}) -- inversion > 0.5"
                )
        prev_date = d
        prev_sev = cur_sev
    outcomes["severity_does_not_invert"] = not sev_inverted
    outcomes["severity_inversions_detail"] = inversion_detail
    outcomes["median_severity_by_date"] = median_sev_by_date

    # --- merge of gates.
    outcomes["all_gates_passed"] = bool(
        merge_passed
        and severity_band_ok
        and not noise_hard_fail
        and not sev_inverted
    )
    return outcomes


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_report_text(report: BackfillReport) -> str:
    """Human-readable report dumped to stdout.

    Per-snapshot row format:
      <date> | samples=K | flags_total=N | fx_hits=M/K | severities=[...]

    Then a per-gate verdict block (PASS/FAIL with the diagnostic detail).
    """
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(
        f"State-observer backfill verification report  ({report.mode})"
    )
    lines.append("=" * 72)
    lines.append(f"user_id            : {report.user_id}")
    lines.append(f"generated_at       : {report.generated_at}")
    lines.append(f"k_samples          : {report.k_samples}")
    lines.append(f"m_fx_required      : {report.m_fx_required}")
    lines.append(f"snapshot_dates     : {', '.join(report.snapshot_dates) or '(none)'}")
    lines.append(f"most_recent_date   : {report.most_recent_date}")
    if report.degraded_dates:
        lines.append(
            f"degraded_dates     : {', '.join(report.degraded_dates)} "
            "(snapshot couldn't be reconstructed; included for visibility)"
        )
    lines.append("")

    # Per-snapshot per-sample table.
    lines.append("Per-snapshot results")
    lines.append("-" * 72)
    by_date: dict[str, list[SnapshotResult]] = {}
    for r in report.results:
        by_date.setdefault(r.snapshot_date, []).append(r)
    for d in sorted(by_date.keys()):
        rows = sorted(by_date[d], key=lambda r: r.sample_idx)
        fx_hits = sum(1 for r in rows if r.fx_flag_surfaced)
        severities = [r.fx_flag_severity or "-" for r in rows]
        total_flags = [r.flag_candidates_count for r in rows]
        lines.append(
            f"  {d}  K={len(rows):>2}  flags_per_sample={total_flags}  "
            f"fx_hits={fx_hits}/{len(rows)}  severities={severities}"
        )
        for r in rows:
            if r.degraded_reason:
                lines.append(
                    f"    sample[{r.sample_idx}] degraded: {r.degraded_reason}"
                )

    lines.append("")
    lines.append("Acceptance gates (Appendix C.2)")
    lines.append("-" * 72)
    g = report.gate_outcomes or {}
    lines.append(
        f"  [merge_gate]                {'PASS' if g.get('merge_gate_fx_on_most_recent') else 'FAIL'}"
    )
    lines.append(f"    {g.get('merge_gate_detail', '(no detail)')}")
    lines.append(
        f"  [severity_band]             {'PASS' if g.get('severity_band_ok_on_most_recent') else 'FAIL'}"
    )
    lines.append(f"    {g.get('severity_band_detail', '(no detail)')}")
    lines.append(
        f"  [severity_does_not_invert]  {'PASS' if g.get('severity_does_not_invert') else 'FAIL'}"
    )
    if g.get("severity_inversions_detail"):
        for d in g["severity_inversions_detail"]:
            lines.append(f"    inversion: {d}")
    lines.append(
        f"  [noise_slo]                 {'PASS' if not g.get('noise_hard_fail') else 'FAIL (hard)'}"
    )
    for date_iso, slo in (g.get("noise_slo") or {}).items():
        lines.append(
            f"    {date_iso}: median={slo['median']} samples={slo['samples']} "
            f"status={slo['status']}"
        )
    lines.append("")
    lines.append(
        f"OVERALL: {'PASS — architecture verified' if g.get('all_gates_passed') else 'FAIL — see above; iterate prompt before merging'}"
    )
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Snapshot reconstruction (real-LLM path only)
# ---------------------------------------------------------------------------


def _reconstruct_state_inputs(
    *,
    user_id: str,
    as_of: date,
    trigger_reason: str = "backfill",
) -> tuple[dict[str, Any], str | None]:
    """Build the ``agent.run(**kwargs)`` payload for a real-LLM call.

    Returns ``(kwargs, degraded_reason)``. When ``degraded_reason`` is
    non-empty the caller skips the call and records the cell as
    degraded; ``kwargs`` is still returned (empty-ish) for type safety.

    Only used in ``--real-llm`` mode; the dry-run path doesn't need
    a real snapshot since the fake agent ignores most kwargs.
    """
    # Imported lazily so dry-run runs don't pay the import cost +
    # don't require the DB to be reachable.
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from argosy.services.state_snapshot import (
            collect_state_snapshot,
            get_latest_state_snapshot,
            state_snapshot_to_dict,
            StateReplayError,
        )
        from argosy.services.state_diff import compute_full_diff
    except ImportError as exc:
        return {}, f"import_failed: {exc}"

    try:
        # We need a sync session; tests / scripts use the same
        # connection string as the orchestrator.
        #
        # Codex IMPORTANT #1 (2026-05-29 review): the default DB path
        # is resolved RELATIVE TO THE REPO ROOT, not the script's CWD.
        # `python -m argosy.scripts.state_observer_backfill` invoked
        # from any directory should locate the same `db/argosy.db`.
        # We anchor at this file's location (argosy/scripts/) and
        # walk up two parents -> repo root.
        import os
        default_db_path = (
            Path(__file__).resolve().parents[2] / "db" / "argosy.db"
        )
        db_url = os.environ.get(
            "ARGOSY_DB_URL",
            f"sqlite:///{default_db_path}",
        )
        # sync_session — the snapshot service is sync.
        engine = create_engine(db_url, future=True)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        session = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        return {}, f"session_setup_failed: {type(exc).__name__}: {exc}"

    try:
        try:
            snap_dict = collect_state_snapshot(
                session, user_id, as_of=as_of, trigger_reason=trigger_reason,
            )
        except StateReplayError as exc:
            return {}, f"state_replay_error: {exc}"
        except Exception as exc:  # noqa: BLE001
            return {}, f"collect_failed: {type(exc).__name__}: {exc}"

        state = snap_dict.get("state") or {}
        source_versions = snap_dict.get("source_versions") or {}

        # Plan baseline: the plan_inputs section the diff is computed
        # against. We provide it as a six-section-shaped dict.
        plan_inputs = state.get("plan_inputs") or {}
        plan_baseline = {
            "plan_inputs": plan_inputs,
            "portfolio": {
                "allocations": (state.get("portfolio") or {}).get("allocations") or [],
            },
        }

        # Prior snapshot: most recent state_snapshots row BEFORE as_of.
        prior_state = None
        latest = get_latest_state_snapshot(session, user_id)
        if latest is not None and latest.snapshot_date and latest.snapshot_date < as_of:
            prior_state = (state_snapshot_to_dict(latest) or {}).get("state")

        full_diff = compute_full_diff(state, plan_baseline, prior_state)
        # Serialise FieldDiff rows for the agent.
        full_diff_dict = {
            "vs_plan": [
                {
                    "path": fd.path,
                    "current_value": fd.current_value,
                    "baseline_value": fd.baseline_value,
                    "deviation_kind": fd.deviation_kind,
                    "magnitude": fd.magnitude,
                    "baseline_label": fd.baseline_label,
                }
                for fd in full_diff.vs_plan
            ],
            "vs_prior": [
                {
                    "path": fd.path,
                    "current_value": fd.current_value,
                    "baseline_value": fd.baseline_value,
                    "deviation_kind": fd.deviation_kind,
                    "magnitude": fd.magnitude,
                    "baseline_label": fd.baseline_label,
                }
                for fd in full_diff.vs_prior
            ],
        }

        # Plan summary: short paragraph of plan_inputs. Per Appendix B
        # this is the authoritative "what the plan assumed" block.
        plan_summary = _summarize_plan_for_prompt(plan_inputs)

        return {
            "plan_summary": plan_summary,
            "current_state": state,
            "plan_baseline": plan_baseline,
            "prior_snapshot": prior_state,
            "full_diff": full_diff_dict,
            "user_notes": "(backfill verification — no live user_notes)",
            "user_id": user_id,
            "snapshot_date": as_of.isoformat(),
            "plan_draft_id": plan_inputs.get("plan_version_id"),
            "trigger_reason": trigger_reason,
            "historical_replay_gaps": list(
                source_versions.get("historical_replay_gaps") or []
            ),
            "diff_truncation_notice": (
                "vs_plan truncated" if full_diff.vs_plan_truncated else
                ("vs_prior truncated" if full_diff.vs_prior_truncated else "")
            ),
            "recent_news_excerpts": (
                (state.get("macro") or {}).get("recent_high_materiality_news") or []
            ),
        }, None
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def _summarize_plan_for_prompt(plan_inputs: dict[str, Any]) -> str:
    """One-paragraph plain-text plan summary for the LLM's
    ``<plan_summary>`` block.

    Per Appendix B: this is the AUTHORITATIVE source of what the plan
    assumed; the LLM is told to treat its contents as the binding
    baseline. Keep it terse but specific -- numbers that matter, not
    prose.
    """
    if not plan_inputs:
        return "(no active plan baseline; the diff has nothing to compare against)"

    parts: list[str] = []
    fx = plan_inputs.get("assumed_fx_usd_nis")
    if fx is not None:
        parts.append(f"assumed USD/NIS = {fx}")
    mu = plan_inputs.get("assumed_mu_nominal_annual")
    if mu is not None:
        parts.append(f"nominal expected return = {mu * 100:.1f}%/yr")
    sigma = plan_inputs.get("assumed_sigma_annual")
    if sigma is not None:
        parts.append(f"return sigma = {sigma * 100:.1f}%/yr")
    infl = plan_inputs.get("assumed_inflation_annual")
    if infl is not None:
        parts.append(f"inflation = {infl * 100:.1f}%/yr")
    retire = plan_inputs.get("assumed_retirement_age")
    if retire is not None:
        parts.append(f"target retirement age = {retire}")
    monthly_expense = plan_inputs.get("assumed_monthly_expenses_nis")
    if monthly_expense is not None:
        parts.append(f"monthly expenses = {monthly_expense:,.0f} NIS")
    monthly_income = plan_inputs.get("assumed_monthly_income_nis")
    if monthly_income is not None:
        parts.append(f"monthly income = {monthly_income:,.0f} NIS")
    target_alloc = plan_inputs.get("assumed_target_allocation") or {}
    if target_alloc:
        alloc_str = ", ".join(f"{k}={v * 100:.0f}%" for k, v in target_alloc.items())
        parts.append(f"target allocation: {alloc_str}")

    if not parts:
        return "(plan_inputs present but no recognisable assumptions to summarise)"
    return "Plan assumptions: " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


async def run_backfill(
    *,
    user_id: str,
    snapshot_dates: list[date],
    k_samples: int,
    m_fx_required: int,
    dry_run: bool,
    fixture_path: str | None = None,
    fake_agent_factory: Any = None,
) -> BackfillReport:
    """Walk ``snapshot_dates`` x K samples and assemble the report.

    Args:
      user_id:        tenant.
      snapshot_dates: ordered ASC; the LAST one is the merge-gate anchor.
      k_samples:      per-snapshot sample count (Appendix C K).
      m_fx_required:  M-of-K threshold for the merge gate.
      dry_run:        True => use fixture-backed fake agent.
                      False => use real :class:`StateObserverAgent`.
      fixture_path:   override for the dry-run fixture path.
      fake_agent_factory: dependency injection for tests; when set,
        called as ``factory(user_id=..., fixture=...)`` instead of
        the default ``_FakeStateObserverAgent`` constructor.

    Returns:
      :class:`BackfillReport` with results + gate_outcomes populated.
    """
    if not snapshot_dates:
        raise ValueError(
            "state_observer_backfill.run_backfill: snapshot_dates is empty; "
            "nothing to verify."
        )

    snapshot_dates_sorted = sorted(snapshot_dates)
    iso_dates = [d.isoformat() for d in snapshot_dates_sorted]

    report = BackfillReport(
        user_id=user_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        mode=("dry-run" if dry_run else "real-llm"),
        k_samples=k_samples,
        m_fx_required=m_fx_required,
        snapshot_dates=iso_dates,
        most_recent_date=iso_dates[-1],
    )

    if dry_run:
        fixture = load_fixture(fixture_path or DEFAULT_FIXTURE_PATH)
        factory = fake_agent_factory or _FakeStateObserverAgent
        # ONE agent per snapshot so the sample idx counter resets per date
        # (per-date dispenser as documented in _FakeStateObserverAgent).
        # Same agent across samples of the SAME date.
        agent_per_date: dict[str, Any] = {
            d_iso: factory(user_id=user_id, fixture=fixture)
            for d_iso in iso_dates
        }
    else:
        # Real-LLM agent — instantiated once per script run; the
        # observer is stateless across runs so we don't need per-date.
        from argosy.agents.state_observer import StateObserverAgent
        agent = StateObserverAgent(user_id=user_id)
        agent_per_date = {d_iso: agent for d_iso in iso_dates}

    # Pre-build state_inputs per date for the real-LLM path. The dry-run
    # path passes only ``snapshot_date`` since the fake agent ignores
    # the rest.
    state_inputs_per_date: dict[str, dict[str, Any]] = {}
    if dry_run:
        for d_iso in iso_dates:
            state_inputs_per_date[d_iso] = {"snapshot_date": d_iso}
    else:
        for d, d_iso in zip(snapshot_dates_sorted, iso_dates):
            kwargs, degraded = _reconstruct_state_inputs(
                user_id=user_id, as_of=d, trigger_reason="backfill",
            )
            if degraded:
                report.degraded_dates.append(d_iso)
                state_inputs_per_date[d_iso] = {
                    "snapshot_date": d_iso,
                    "_degraded_reason": degraded,
                }
            else:
                state_inputs_per_date[d_iso] = kwargs

    # Walk every (date, sample) cell.
    for d_iso in iso_dates:
        agent = agent_per_date[d_iso]
        state_inputs_base = state_inputs_per_date.get(d_iso, {"snapshot_date": d_iso})
        degraded_reason = state_inputs_base.pop("_degraded_reason", None)
        # Ensure the snapshot_date is propagated even for the real-LLM
        # path -- the fake-agent dispatch is by date, and even the real
        # agent gets metadata for the prompt.
        state_inputs_base.setdefault("snapshot_date", d_iso)

        for k in range(k_samples):
            if degraded_reason:
                report.results.append(SnapshotResult(
                    snapshot_date=d_iso,
                    sample_idx=k,
                    flag_candidates_count=0,
                    fx_flag_surfaced=False,
                    fx_flag_severity=None,
                    overall_assessment="",
                    confidence=None,
                    degraded_reason=degraded_reason,
                ))
                continue

            result = await run_observer_on_snapshot(
                agent=agent,
                snapshot_date_iso=d_iso,
                sample_idx=k,
                state_inputs=state_inputs_base,
            )
            report.results.append(result)

    report.gate_outcomes = evaluate_acceptance_gates(report)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="state_observer_backfill",
        description=(
            "Run the state-observer agent against N historical snapshots "
            "and assert the FX 3.6 -> 2.8 case surfaces as an emergent "
            "flag (Spec B §5 / Appendix C). The empirical merge gate."
        ),
    )
    parser.add_argument(
        "--user-id", default="ariel",
        help="Tenant to backfill for (default: ariel).",
    )
    parser.add_argument(
        "--snapshots", type=int, default=DEFAULT_N_SNAPSHOTS,
        help=f"How many historical anchors to walk (default: {DEFAULT_N_SNAPSHOTS}).",
    )
    parser.add_argument(
        "--interval-days", type=int, default=DEFAULT_INTERVAL_DAYS,
        help=f"Days between auto-discovered anchors (default: {DEFAULT_INTERVAL_DAYS}).",
    )
    parser.add_argument(
        "--as-of-dates", default="",
        help=(
            "Comma-separated ISO dates that OVERRIDE auto-discovery "
            "(e.g. '2026-05-29,2026-04-29'). The last date in the list "
            "is treated as the merge-gate anchor."
        ),
    )
    parser.add_argument(
        "--k-samples", type=int, default=DEFAULT_K_SAMPLES,
        help=f"Samples per snapshot (Appendix C K; default {DEFAULT_K_SAMPLES}).",
    )
    parser.add_argument(
        "--m-fx-required", type=int, default=DEFAULT_M_FX_REQUIRED,
        help=(
            "Required FX-flag surfaces on the most-recent date "
            f"(Appendix C M; default {DEFAULT_M_FX_REQUIRED})."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", default=True,
        help=(
            "Use the fixture-backed fake agent — no Opus calls (default). "
            "This is what CI runs."
        ),
    )
    mode.add_argument(
        "--real-llm", action="store_true",
        help=(
            "Opt in to real Opus calls. Per [[feedback_accuracy_over_cost]] "
            "this is the binding-tolerant mode for manual verification "
            "before merge."
        ),
    )
    parser.add_argument(
        "--fixture", default=DEFAULT_FIXTURE_PATH,
        help=f"Dry-run fixture path (default: {DEFAULT_FIXTURE_PATH}).",
    )
    parser.add_argument(
        "--report-out", default="",
        help=(
            "Write JSON report to this path in addition to stdout. "
            "Default: stdout only."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Verbose logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code (0 pass, 1 fail).

    Exit code semantics:
      0 -- all gates passed (architecture verified).
      1 -- at least one gate failed (iterate prompt + re-run before merge).
      2 -- script-level error (bad CLI args, missing fixture).
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    # mutex: --real-llm overrides the default --dry-run.
    dry_run = not args.real_llm

    explicit_dates: list[str] | None = None
    if args.as_of_dates.strip():
        explicit_dates = [s.strip() for s in args.as_of_dates.split(",") if s.strip()]

    try:
        snapshot_dates = discover_snapshot_dates(
            n_snapshots=args.snapshots,
            interval_days=args.interval_days,
            explicit_dates=explicit_dates,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        report = asyncio.run(run_backfill(
            user_id=args.user_id,
            snapshot_dates=snapshot_dates,
            k_samples=args.k_samples,
            m_fx_required=args.m_fx_required,
            dry_run=dry_run,
            fixture_path=args.fixture if dry_run else None,
        ))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Print the human-readable report regardless of pass/fail.
    print(format_report_text(report))

    if args.report_out:
        out_path = Path(args.report_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8",
        )
        print(f"JSON report written to {out_path}")

    gates_passed = bool((report.gate_outcomes or {}).get("all_gates_passed"))
    return 0 if gates_passed else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "BackfillReport",
    "SnapshotResult",
    "DEFAULT_FIXTURE_PATH",
    "DEFAULT_K_SAMPLES",
    "DEFAULT_M_FX_REQUIRED",
    "DEFAULT_N_SNAPSHOTS",
    "DEFAULT_INTERVAL_DAYS",
    "ACCEPTABLE_FX_SEVERITIES",
    "NOISE_MEDIAN_SLO",
    "NOISE_MEDIAN_HARD_FAIL",
    "_FakeStateObserverAgent",
    "load_fixture",
    "discover_snapshot_dates",
    "run_observer_on_snapshot",
    "evaluate_acceptance_gates",
    "format_report_text",
    "run_backfill",
    "main",
]
