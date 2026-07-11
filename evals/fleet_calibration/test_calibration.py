"""Pytest entries for the fleet-calibration benchmark.

- test_packet_audits: fast, no LLM — every committed packet must pass the
  temporal-integrity + schema + no-calendar-years audits (T4-style intentional
  temporal disqualifications are allowlisted).
- test_suite_live: marked llm_eval — runs the full suite (hours of real Opus
  calls). Excluded from the default suite; run explicitly:
      .venv/Scripts/python.exe -m pytest -m llm_eval evals/fleet_calibration
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_suite import packet_year_audit, temporal_audit  # noqa: E402

# Points whose packets are KNOWN temporal violations kept on record (the audit's
# catches, per the spec's TTCF T4 note). They must stay disqualified, not fixed.
EXPECTED_TEMPORAL_DISQUALIFIED = {"ttcf_t4"}

REQUIRED_KEYS = {
    "case_id", "alias", "category", "grading", "real", "expected_classes",
    "positioned", "positions", "contamination_terms", "analyst_reports",
}


def _packets():
    return sorted((HERE / "packets").glob("*.json"))


@pytest.mark.parametrize("path", _packets(), ids=lambda p: p.stem)
def test_packet_audits(path: Path) -> None:
    pkt = json.loads(path.read_text(encoding="utf-8"))
    missing = REQUIRED_KEYS - set(pkt)
    assert not missing, f"missing keys: {missing}"
    assert pkt["case_id"] == path.stem
    years = packet_year_audit(pkt)
    assert not years, f"calendar years leak into analyst_reports: {years}"
    violations = temporal_audit(pkt)
    if pkt["case_id"] in EXPECTED_TEMPORAL_DISQUALIFIED:
        assert violations, "expected a recorded temporal violation"
    else:
        assert not violations, f"temporal-integrity violations: {violations}"
    if not pkt.get("synthetic"):
        assert pkt.get("rescale_factor"), "real cases must carry a rescale factor"
        assert pkt.get("resolution"), "real cases must carry a resolution block"


@pytest.mark.llm_eval
def test_suite_live(tmp_path: Path) -> None:
    out = tmp_path / "run.json"
    proc = subprocess.run(
        [sys.executable, str(HERE / "run_suite.py"), "--out", str(out)],
        capture_output=True, text=True, timeout=4 * 3600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    doc = json.loads(out.read_text(encoding="utf-8"))
    ok = [r for r in doc["results"] if r.get("status") == "ok"]
    assert ok, "no points ran"
    # synthetic control pair is the tripwire (spec protocol step 3)
    syn = {r["case_id"]: r for r in ok if r["case_id"].endswith("_synthetic")}
    if "qbt_synthetic" in syn:
        assert syn["qbt_synthetic"]["action"] == "buy", "synthetic winner-shape failed — lens regressed"
    if "srl_synthetic" in syn:
        assert syn["srl_synthetic"]["action"] == "hold", "synthetic trap-shape failed — lens regressed"
