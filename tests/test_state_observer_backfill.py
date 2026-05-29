"""Tests for argosy/scripts/state_observer_backfill.py (Spec B commit #5).

These tests exercise:

  * The dry-run path using the fixture-backed fake agent — the report
    logic + gate arithmetic without any Opus call.
  * The acceptance gate FAILS LOUDLY when fewer than M=4 of K=5
    samples on the most-recent snapshot surface an FX flag.
  * The acceptance gate FAILS LOUDLY when the most-recent snapshot
    surfaces FX only at 'info' severity (severity_band check).
  * The fixture loads correctly + ``_FakeStateObserverAgent`` returns
    deterministic outputs across runs (byte-identical samples per
    (date, idx) cell).
  * Degraded-mode behavior: when a snapshot date isn't present in the
    fixture the fake agent emits an empty output + the report flags
    the cell.
  * ``main()`` returns exit code 1 when the gates fail and 0 when
    they pass.

All tests are dry-run; none mark `llm_eval`, so the default
`pytest -m "not llm_eval"` includes them. Real-LLM verification is
manual via ``python -m argosy.scripts.state_observer_backfill --real-llm``.
"""

from __future__ import annotations

import asyncio
import io
import json
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from argosy.scripts.state_observer_backfill import (
    DEFAULT_FIXTURE_PATH,
    DEFAULT_K_SAMPLES,
    DEFAULT_M_FX_REQUIRED,
    DEFAULT_N_SNAPSHOTS,
    BackfillReport,
    SnapshotResult,
    _FakeStateObserverAgent,
    discover_snapshot_dates,
    evaluate_acceptance_gates,
    format_report_text,
    load_fixture,
    main,
    run_backfill,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / DEFAULT_FIXTURE_PATH


# ---------------------------------------------------------------------------
# Fixture sanity
# ---------------------------------------------------------------------------


def test_fixture_loads_and_has_five_snapshots() -> None:
    """The fixture file must exist + have the K=5 contract shape.

    If this fails, the dry-run path is broken and the merge gate
    cannot be exercised in CI. Loud failure preferred.
    """
    data = load_fixture(FIXTURE_PATH)
    assert "snapshots" in data
    assert isinstance(data["snapshots"], list)
    assert len(data["snapshots"]) == 5, (
        f"Expected 5 snapshot entries; got {len(data['snapshots'])}. "
        "The fixture should walk the FX 3.6 -> 2.8 progression in 5 monthly "
        "steps per spec §5.3."
    )
    for snap in data["snapshots"]:
        assert "snapshot_date" in snap
        assert "samples" in snap
        assert len(snap["samples"]) == DEFAULT_K_SAMPLES, (
            f"Snapshot {snap.get('snapshot_date')!r} has "
            f"{len(snap['samples'])} samples; Appendix C K={DEFAULT_K_SAMPLES} "
            "contract requires exactly this many per snapshot."
        )


def test_fixture_most_recent_snapshot_drives_merge_gate() -> None:
    """The most-recent snapshot in the fixture must show the FX flag
    in M=4+ of K=5 samples — that's the merge-gate empirical contract.
    """
    data = load_fixture(FIXTURE_PATH)
    # Snapshots are sorted by ISO date; max is most recent.
    most_recent = max(data["snapshots"], key=lambda s: s["snapshot_date"])
    fx_at_warning_or_critical = 0
    for sample in most_recent["samples"]:
        for cand in sample.get("flag_candidates") or []:
            if (cand.get("primary_field", "").startswith("macro.fx_")
                    and cand.get("severity") in ("warning", "critical")):
                fx_at_warning_or_critical += 1
                break
    assert fx_at_warning_or_critical >= DEFAULT_M_FX_REQUIRED, (
        f"Most-recent snapshot in fixture has {fx_at_warning_or_critical} "
        f"FX surfaces at warning|critical, below M={DEFAULT_M_FX_REQUIRED}. "
        "The dry-run merge gate would FAIL on this fixture, which would "
        "be a self-test failure — fix the fixture, not the gate."
    )


# ---------------------------------------------------------------------------
# Fake agent determinism
# ---------------------------------------------------------------------------


def test_fake_agent_deterministic_across_runs() -> None:
    """The dry-run agent must dispense byte-identical outputs across
    runs given the same (date, sample_idx) inputs. This is the
    reproducibility contract."""
    fixture = load_fixture(FIXTURE_PATH)
    agent1 = _FakeStateObserverAgent(user_id="ariel", fixture=fixture)
    agent2 = _FakeStateObserverAgent(user_id="ariel", fixture=fixture)

    async def _drive(agent: Any, n: int) -> list[dict[str, Any]]:
        out = []
        for _ in range(n):
            report = await agent.run(snapshot_date="2026-05-29")
            out.append({
                "candidates": [c.model_dump() for c in report.output.flag_candidates],
                "assessment": report.output.overall_assessment,
                "confidence": report.output.confidence.value,
            })
        return out

    r1 = asyncio.run(_drive(agent1, DEFAULT_K_SAMPLES))
    r2 = asyncio.run(_drive(agent2, DEFAULT_K_SAMPLES))
    assert r1 == r2, (
        "Fake agent emitted different outputs across two identical runs; "
        "this breaks the determinism contract that lets CI rely on the "
        "dry-run gate."
    )


def test_fake_agent_returns_empty_for_unknown_date() -> None:
    """Missing dates fall through to an empty StateObserverOutput; the
    fake agent does NOT raise. This is the degraded-mode contract."""
    fixture = load_fixture(FIXTURE_PATH)
    agent = _FakeStateObserverAgent(user_id="ariel", fixture=fixture)

    async def _run() -> Any:
        return await agent.run(snapshot_date="1999-01-01")

    report = asyncio.run(_run())
    assert len(report.output.flag_candidates) == 0
    assert "no fixture data" in report.output.overall_assessment


def test_fake_agent_returns_empty_when_sample_idx_overflows() -> None:
    """When a date HAS data but the K-counter goes past len(samples),
    the agent emits an empty output rather than wrapping/indexing
    incorrectly. Caller observes the cell as "no flags emitted" and
    the merge gate naturally fails."""
    fixture = load_fixture(FIXTURE_PATH)
    agent = _FakeStateObserverAgent(user_id="ariel", fixture=fixture)

    async def _drive() -> list[int]:
        counts: list[int] = []
        # Run 10 samples (fixture has 5); samples 0-4 should fill,
        # 5-9 should be empty.
        for _ in range(10):
            report = await agent.run(snapshot_date="2026-05-29")
            counts.append(len(report.output.flag_candidates))
        return counts

    counts = asyncio.run(_drive())
    assert len(counts) == 10
    # The first 5 are populated; the next 5 fall through to empty.
    assert all(c > 0 for c in counts[:5])
    assert all(c == 0 for c in counts[5:])


# ---------------------------------------------------------------------------
# Date discovery
# ---------------------------------------------------------------------------


def test_discover_snapshot_dates_explicit_overrides_auto() -> None:
    dates = discover_snapshot_dates(
        n_snapshots=1,  # would be ignored
        interval_days=30,
        explicit_dates=["2026-05-29", "2026-04-29", "2026-03-30"],
    )
    assert dates == [
        date(2026, 3, 30),
        date(2026, 4, 29),
        date(2026, 5, 29),
    ]


def test_discover_snapshot_dates_auto_walks_backwards() -> None:
    anchor = date(2026, 5, 29)
    dates = discover_snapshot_dates(
        n_snapshots=3, interval_days=30,
        anchor_today=anchor,
    )
    assert dates == [
        anchor - timedelta(days=60),
        anchor - timedelta(days=30),
        anchor,
    ]


def test_discover_snapshot_dates_rejects_zero() -> None:
    with pytest.raises(ValueError, match="--snapshots must be"):
        discover_snapshot_dates(n_snapshots=0, interval_days=30)


def test_discover_snapshot_dates_rejects_bad_iso() -> None:
    with pytest.raises(ValueError, match="invalid ISO date"):
        discover_snapshot_dates(
            n_snapshots=1, interval_days=30,
            explicit_dates=["2026-05-29", "notadate"],
        )


# ---------------------------------------------------------------------------
# End-to-end dry-run — happy path
# ---------------------------------------------------------------------------


def _fixture_dates_asc() -> list[date]:
    data = load_fixture(FIXTURE_PATH)
    return sorted(date.fromisoformat(s["snapshot_date"]) for s in data["snapshots"])


def test_dry_run_happy_path_all_gates_pass() -> None:
    """Drive the full backfill against the fixture. All gates should
    pass — the fixture is designed to satisfy them."""
    dates = _fixture_dates_asc()
    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=dates,
        k_samples=DEFAULT_K_SAMPLES,
        m_fx_required=DEFAULT_M_FX_REQUIRED,
        dry_run=True,
        fixture_path=str(FIXTURE_PATH),
    ))
    g = report.gate_outcomes
    assert g["merge_gate_fx_on_most_recent"] is True, g.get("merge_gate_detail")
    assert g["severity_band_ok_on_most_recent"] is True, g.get("severity_band_detail")
    assert g["severity_does_not_invert"] is True, g.get("severity_inversions_detail")
    assert g["noise_hard_fail"] is False
    assert g["all_gates_passed"] is True


def test_dry_run_records_per_snapshot_results() -> None:
    """The report should contain K*N rows (5 samples x 5 snapshots = 25)."""
    dates = _fixture_dates_asc()
    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=dates,
        k_samples=DEFAULT_K_SAMPLES,
        m_fx_required=DEFAULT_M_FX_REQUIRED,
        dry_run=True,
        fixture_path=str(FIXTURE_PATH),
    ))
    assert len(report.results) == DEFAULT_K_SAMPLES * len(dates)
    for r in report.results:
        assert isinstance(r, SnapshotResult)
        assert r.snapshot_date in {d.isoformat() for d in dates}
        assert 0 <= r.sample_idx < DEFAULT_K_SAMPLES


def test_dry_run_report_text_includes_pass_verdict() -> None:
    """The human-readable report block must visibly say PASS when
    gates pass — that's the operator-visible status line."""
    dates = _fixture_dates_asc()
    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=dates,
        k_samples=DEFAULT_K_SAMPLES,
        m_fx_required=DEFAULT_M_FX_REQUIRED,
        dry_run=True,
        fixture_path=str(FIXTURE_PATH),
    ))
    text = format_report_text(report)
    assert "PASS — architecture verified" in text
    assert "merge_gate" in text
    assert "severity_band" in text


# ---------------------------------------------------------------------------
# Negative tests — gate failure modes
# ---------------------------------------------------------------------------


def _write_fixture_subset(
    tmp_path: Path,
    *,
    most_recent_samples: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a single-snapshot fixture with the given most-recent
    samples; used to drive failure modes in the merge gate."""
    fx = {
        "fixture_version": "test",
        "fixture_kind": "test",
        "doc": ["single-snapshot smoke for gate failure tests"],
        "plan_baseline_fx_usd_nis": 3.6,
        "snapshots": [
            {
                "snapshot_date": "2026-05-29",
                "samples": most_recent_samples or [],
            }
        ],
    }
    out = tmp_path / "fixture.json"
    out.write_text(json.dumps(fx), encoding="utf-8")
    return out


def test_merge_gate_fails_when_fewer_than_m_fx_surfaces(tmp_path: Path) -> None:
    """K=5 samples but only 2 surface FX at warning|critical -> merge
    gate FAILS. The script's exit code must be 1."""
    samples = [
        # 2 with FX at warning, 3 with no flags
        {
            "flag_candidates": [{
                "severity": "warning", "primary_field": "macro.fx_usd_nis_spot",
                "rationale_md": "x", "inferred_kind": "fx_observation",
                "deviation_bucket": "large", "confidence": "HIGH",
            }],
            "overall_assessment": "", "confidence": "HIGH", "cited_sources": [],
        },
        {
            "flag_candidates": [{
                "severity": "critical", "primary_field": "macro.fx_usd_nis_spot",
                "rationale_md": "x", "inferred_kind": "fx_observation",
                "deviation_bucket": "large", "confidence": "HIGH",
            }],
            "overall_assessment": "", "confidence": "HIGH", "cited_sources": [],
        },
        {"flag_candidates": [], "overall_assessment": "", "confidence": "MEDIUM", "cited_sources": []},
        {"flag_candidates": [], "overall_assessment": "", "confidence": "MEDIUM", "cited_sources": []},
        {"flag_candidates": [], "overall_assessment": "", "confidence": "MEDIUM", "cited_sources": []},
    ]
    fpath = _write_fixture_subset(tmp_path, most_recent_samples=samples)

    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=[date(2026, 5, 29)],
        k_samples=5,
        m_fx_required=4,
        dry_run=True,
        fixture_path=str(fpath),
    ))
    g = report.gate_outcomes
    assert g["merge_gate_fx_on_most_recent"] is False
    assert "2/5" in g["merge_gate_detail"]
    assert g["all_gates_passed"] is False


def test_merge_gate_fails_when_fx_only_at_info_severity(tmp_path: Path) -> None:
    """5/5 surfaces but all at 'info' severity -> merge gate FAILS
    because the severity-band check rejects info on the most-recent
    snapshot. This catches the "false-pass" where the observer flagged
    something FX-shaped but didn't classify it as material."""
    info_sample = {
        "flag_candidates": [{
            "severity": "info", "primary_field": "macro.fx_usd_nis_spot",
            "rationale_md": "x", "inferred_kind": "fx_observation",
            "deviation_bucket": "small", "confidence": "MEDIUM",
        }],
        "overall_assessment": "", "confidence": "MEDIUM", "cited_sources": [],
    }
    samples = [deepcopy(info_sample) for _ in range(5)]
    fpath = _write_fixture_subset(tmp_path, most_recent_samples=samples)

    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=[date(2026, 5, 29)],
        k_samples=5,
        m_fx_required=4,
        dry_run=True,
        fixture_path=str(fpath),
    ))
    g = report.gate_outcomes
    # Merge gate: 0 surfaces at warning|critical because all are info.
    assert g["merge_gate_fx_on_most_recent"] is False
    # Severity-band gate also fails (info is not allowed).
    assert g["severity_band_ok_on_most_recent"] is False
    assert g["all_gates_passed"] is False


def test_merge_gate_catches_wrong_field_false_pass(tmp_path: Path) -> None:
    """Observer flags `portfolio.top_concentration_pct` but NO FX flag.
    Should still fail the merge gate — this is the "false-pass" the
    codex reviewer asked to confirm doesn't slip through."""
    non_fx_sample = {
        "flag_candidates": [{
            "severity": "critical",
            "primary_field": "portfolio.top_concentration_pct",
            "rationale_md": "x",
            "inferred_kind": "concentration_observation",
            "deviation_bucket": "large",
            "confidence": "HIGH",
        }],
        "overall_assessment": "", "confidence": "HIGH", "cited_sources": [],
    }
    samples = [deepcopy(non_fx_sample) for _ in range(5)]
    fpath = _write_fixture_subset(tmp_path, most_recent_samples=samples)

    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=[date(2026, 5, 29)],
        k_samples=5,
        m_fx_required=4,
        dry_run=True,
        fixture_path=str(fpath),
    ))
    g = report.gate_outcomes
    assert g["merge_gate_fx_on_most_recent"] is False
    assert g["all_gates_passed"] is False
    # The detail should mention the actual count (0/5 FX surfaces).
    assert "0/5" in g["merge_gate_detail"]


def test_noise_slo_hard_fail_when_median_too_high(tmp_path: Path) -> None:
    """A snapshot whose median flag count > 5 fails the noise gate."""
    # Build a sample with 6 flag_candidates (above NOISE_MEDIAN_HARD_FAIL=5).
    fx_flag = {
        "severity": "critical", "primary_field": "macro.fx_usd_nis_spot",
        "rationale_md": "x", "inferred_kind": "fx_observation",
        "deviation_bucket": "large", "confidence": "HIGH",
    }
    noisy_flag = {
        "severity": "info", "primary_field": "macro.vix",
        "rationale_md": "x", "inferred_kind": "volatility_observation",
        "deviation_bucket": "small", "confidence": "LOW",
    }
    noisy_sample = {
        "flag_candidates": [fx_flag] + [noisy_flag] * 5,  # 6 flags total
        "overall_assessment": "", "confidence": "HIGH", "cited_sources": [],
    }
    samples = [deepcopy(noisy_sample) for _ in range(5)]
    fpath = _write_fixture_subset(tmp_path, most_recent_samples=samples)

    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=[date(2026, 5, 29)],
        k_samples=5,
        m_fx_required=4,
        dry_run=True,
        fixture_path=str(fpath),
    ))
    g = report.gate_outcomes
    assert g["noise_hard_fail"] is True
    assert g["all_gates_passed"] is False


# ---------------------------------------------------------------------------
# Acceptance gate unit tests
# ---------------------------------------------------------------------------


def test_evaluate_acceptance_gates_synthetic_pass() -> None:
    """Hand-build a report where all gates should pass."""
    report = BackfillReport(
        user_id="ariel",
        generated_at="2026-05-29T00:00:00Z",
        mode="dry-run",
        k_samples=5,
        m_fx_required=4,
        snapshot_dates=["2026-04-29", "2026-05-29"],
        most_recent_date="2026-05-29",
        results=[
            # Older snapshot: 3 of 5 warning, 2 of 5 info.
            SnapshotResult(snapshot_date="2026-04-29", sample_idx=0,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="warning"),
            SnapshotResult(snapshot_date="2026-04-29", sample_idx=1,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="warning"),
            SnapshotResult(snapshot_date="2026-04-29", sample_idx=2,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="warning"),
            SnapshotResult(snapshot_date="2026-04-29", sample_idx=3,
                           flag_candidates_count=1, fx_flag_surfaced=False),
            SnapshotResult(snapshot_date="2026-04-29", sample_idx=4,
                           flag_candidates_count=1, fx_flag_surfaced=False),
            # Most-recent snapshot: 5 of 5 critical.
            SnapshotResult(snapshot_date="2026-05-29", sample_idx=0,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="critical"),
            SnapshotResult(snapshot_date="2026-05-29", sample_idx=1,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="critical"),
            SnapshotResult(snapshot_date="2026-05-29", sample_idx=2,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="critical"),
            SnapshotResult(snapshot_date="2026-05-29", sample_idx=3,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="critical"),
            SnapshotResult(snapshot_date="2026-05-29", sample_idx=4,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="critical"),
        ],
    )
    outcomes = evaluate_acceptance_gates(report)
    assert outcomes["merge_gate_fx_on_most_recent"] is True
    assert outcomes["severity_band_ok_on_most_recent"] is True
    assert outcomes["severity_does_not_invert"] is True
    assert outcomes["noise_hard_fail"] is False
    assert outcomes["all_gates_passed"] is True


def test_evaluate_acceptance_gates_detects_severity_inversion() -> None:
    """When older snapshot has higher median FX severity than newer,
    the severity-non-decreasing gate fails."""
    report = BackfillReport(
        user_id="ariel",
        generated_at="2026-05-29T00:00:00Z",
        mode="dry-run",
        k_samples=2,
        m_fx_required=1,
        snapshot_dates=["2026-04-29", "2026-05-29"],
        most_recent_date="2026-05-29",
        results=[
            # Older snapshot: critical
            SnapshotResult(snapshot_date="2026-04-29", sample_idx=0,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="critical"),
            SnapshotResult(snapshot_date="2026-04-29", sample_idx=1,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="critical"),
            # Newer snapshot: info (regression — severity dropped)
            SnapshotResult(snapshot_date="2026-05-29", sample_idx=0,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="info"),
            SnapshotResult(snapshot_date="2026-05-29", sample_idx=1,
                           flag_candidates_count=1, fx_flag_surfaced=True,
                           fx_flag_severity="info"),
        ],
    )
    outcomes = evaluate_acceptance_gates(report)
    assert outcomes["severity_does_not_invert"] is False
    assert outcomes["all_gates_passed"] is False


# ---------------------------------------------------------------------------
# Degraded-mode: missing dates / no fixture
# ---------------------------------------------------------------------------


def test_dry_run_handles_missing_dates_gracefully(tmp_path: Path) -> None:
    """When the requested snapshot dates aren't in the fixture, the
    fake agent emits empties — the merge gate fails LOUDLY rather
    than silently passing on no data."""
    # Fixture with just one (irrelevant) date.
    fpath = _write_fixture_subset(tmp_path, most_recent_samples=[])
    # but request a different date.
    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=[date(2099, 12, 31)],
        k_samples=DEFAULT_K_SAMPLES,
        m_fx_required=DEFAULT_M_FX_REQUIRED,
        dry_run=True,
        fixture_path=str(fpath),
    ))
    g = report.gate_outcomes
    assert g["merge_gate_fx_on_most_recent"] is False
    assert "0/5" in g["merge_gate_detail"]


def test_load_fixture_raises_when_missing(tmp_path: Path) -> None:
    """Missing fixture path -> FileNotFoundError. Loud, not silent."""
    with pytest.raises(FileNotFoundError, match="fixture not found"):
        load_fixture(tmp_path / "does_not_exist.json")


def test_load_fixture_raises_on_bad_shape(tmp_path: Path) -> None:
    """Top-level missing 'snapshots' -> ValueError."""
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"some_other_key": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing top-level 'snapshots'"):
        load_fixture(p)


def test_run_backfill_rejects_empty_dates() -> None:
    """Empty snapshot_dates list -> ValueError; the empirical contract
    is meaningless without anchors."""
    with pytest.raises(ValueError, match="snapshot_dates is empty"):
        asyncio.run(run_backfill(
            user_id="ariel",
            snapshot_dates=[],
            k_samples=DEFAULT_K_SAMPLES,
            m_fx_required=DEFAULT_M_FX_REQUIRED,
            dry_run=True,
        ))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_main_exits_zero_when_dry_run_passes(capsys: pytest.CaptureFixture) -> None:
    """End-to-end CLI: with the default fixture, exit code 0 + report
    printed to stdout."""
    # Use the explicit dates from the fixture so the CLI walks the
    # exact dates the fixture was designed for.
    data = load_fixture(FIXTURE_PATH)
    dates_csv = ",".join(sorted(s["snapshot_date"] for s in data["snapshots"]))
    rc = main([
        "--user-id", "ariel",
        "--k-samples", str(DEFAULT_K_SAMPLES),
        "--m-fx-required", str(DEFAULT_M_FX_REQUIRED),
        "--as-of-dates", dates_csv,
        "--fixture", str(FIXTURE_PATH),
        # default mode is dry-run; explicit anyway for clarity.
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS — architecture verified" in out


def test_merge_gate_rejects_30d_avg_only_surfaces(tmp_path: Path) -> None:
    """Codex BLOCKER #1 (2026-05-29): the merge gate requires an EXACT
    match on `macro.fx_usd_nis_spot`. Flagging only the 30-day average
    must NOT pass the gate — the architectural claim is about the
    spot-rate deviation, not derived statistics.
    """
    only_30d_avg = {
        "flag_candidates": [{
            "severity": "critical",
            "primary_field": "macro.fx_usd_nis_30d_avg",
            "rationale_md": "x",
            "inferred_kind": "fx_observation",
            "deviation_bucket": "large",
            "confidence": "HIGH",
        }],
        "overall_assessment": "", "confidence": "HIGH", "cited_sources": [],
    }
    samples = [deepcopy(only_30d_avg) for _ in range(5)]
    fpath = _write_fixture_subset(tmp_path, most_recent_samples=samples)

    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=[date(2026, 5, 29)],
        k_samples=5,
        m_fx_required=4,
        dry_run=True,
        fixture_path=str(fpath),
    ))
    g = report.gate_outcomes
    # The 30d_avg flag is not the spot deviation; merge gate must fail.
    assert g["merge_gate_fx_on_most_recent"] is False, (
        "Merge gate accepted `macro.fx_usd_nis_30d_avg` as proof of the "
        "spot-rate FX flag — that's a false-pass. The empirical contract "
        "is the 3.6 -> 2.8 SPOT deviation."
    )
    assert g["all_gates_passed"] is False


def test_merge_gate_picks_max_severity_when_multiple_fx_candidates(
    tmp_path: Path,
) -> None:
    """Codex BLOCKER #2 (2026-05-29): when a sample emits MULTIPLE FX
    candidates (one info, one critical), the merge gate should record
    the MAX severity, not the first-encountered. The recorded severity
    drives the severity_band check.
    """
    sample_with_both_severities = {
        "flag_candidates": [
            {
                "severity": "info",
                "primary_field": "macro.fx_usd_nis_spot",
                "rationale_md": "x", "inferred_kind": "fx_observation",
                "deviation_bucket": "small", "confidence": "LOW",
            },
            {
                "severity": "critical",
                "primary_field": "macro.fx_usd_nis_spot",
                "rationale_md": "x", "inferred_kind": "fx_observation",
                "deviation_bucket": "large", "confidence": "HIGH",
            },
        ],
        "overall_assessment": "", "confidence": "HIGH", "cited_sources": [],
    }
    samples = [deepcopy(sample_with_both_severities) for _ in range(5)]
    fpath = _write_fixture_subset(tmp_path, most_recent_samples=samples)

    report = asyncio.run(run_backfill(
        user_id="ariel",
        snapshot_dates=[date(2026, 5, 29)],
        k_samples=5,
        m_fx_required=4,
        dry_run=True,
        fixture_path=str(fpath),
    ))
    g = report.gate_outcomes
    # Each sample's recorded severity is 'critical' (max of info+critical),
    # so both the merge gate and the severity_band check pass.
    assert g["merge_gate_fx_on_most_recent"] is True
    assert g["severity_band_ok_on_most_recent"] is True
    # Confirm the recorded severity on each result row is 'critical'.
    for r in report.results:
        assert r.fx_flag_surfaced is True
        assert r.fx_flag_severity == "critical", (
            "Result row recorded fx_flag_severity={r.fx_flag_severity!r}; "
            "expected 'critical' (the max across the two candidates)."
        )


def test_explicit_dates_reject_duplicates() -> None:
    """Codex IMPORTANT #2 (2026-05-29): silent dedup hides operator
    typos. Same date twice in --as-of-dates -> ValueError."""
    with pytest.raises(ValueError, match="duplicate dates"):
        discover_snapshot_dates(
            n_snapshots=1, interval_days=30,
            explicit_dates=["2026-05-29", "2026-05-29"],
        )


def test_main_exits_one_when_gates_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end CLI: gates fail -> exit 1."""
    # All-info fixture (severity_band fails).
    info_sample = {
        "flag_candidates": [{
            "severity": "info", "primary_field": "macro.fx_usd_nis_spot",
            "rationale_md": "x", "inferred_kind": "fx_observation",
            "deviation_bucket": "small", "confidence": "MEDIUM",
        }],
        "overall_assessment": "", "confidence": "MEDIUM", "cited_sources": [],
    }
    samples = [deepcopy(info_sample) for _ in range(5)]
    fpath = _write_fixture_subset(tmp_path, most_recent_samples=samples)

    rc = main([
        "--as-of-dates", "2026-05-29",
        "--k-samples", "5",
        "--m-fx-required", "4",
        "--fixture", str(fpath),
        "--dry-run",
    ])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
