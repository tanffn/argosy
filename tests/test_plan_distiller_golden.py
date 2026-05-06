"""Golden-corpus test: distill the Jacobs plan excerpt and assert content
floor + exclusion rules.

This test calls the actual LLM (PlanDistillerAgent.run_sync against
the configured backend — either the Claude Agent SDK "claude_code" path
or the direct Anthropic API "api_key" path).  It is marked ``llm_eval``
so CI can skip it when no live LLM backend is reachable, but it MUST
pass before Wave 1 ships per the wave gate (SDD §14.6).

Run locally with::

    pytest tests/test_plan_distiller_golden.py -m llm_eval -v

The test auto-skips when:
  - backend == "api_key" and ANTHROPIC_API_KEY is not set
  - backend == "claude_code" and ``claude.exe`` is not on PATH
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

EXCERPT = Path("tests/golden/jacobs_plan_excerpt.md")
EXPECT = Path("tests/golden/jacobs_distillate_expected.json")


def _llm_backend_available() -> bool:
    """Return True when at least one LLM backend is reachable."""
    try:
        from argosy.config import get_settings

        settings = get_settings()
        backend = settings.anthropic.backend
    except Exception:
        backend = "claude_code"  # default per argosy.toml

    if backend == "api_key":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if backend == "claude_code":
        return shutil.which("claude") is not None
    return False


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not _llm_backend_available(),
    reason=(
        "No LLM backend reachable: set ANTHROPIC_API_KEY (api_key backend) "
        "or ensure claude.exe is on PATH (claude_code backend)"
    ),
)
def test_distiller_produces_acceptable_distillate_for_jacobs_excerpt():
    from argosy.agents.plan_distiller import PlanDistillerAgent

    plan_md = EXCERPT.read_text(encoding="utf-8")
    expect = json.loads(EXPECT.read_text(encoding="utf-8"))

    agent = PlanDistillerAgent(user_id="test")
    result = agent.run_sync(plan_label="Jacobs v2.0 (excerpt)", plan_markdown=plan_md)
    distillate = result.output

    serialized = distillate.model_dump_json().lower()

    def _norm(s: str) -> str:
        """Normalise label for fuzzy matching: lower-case, collapse _ to space."""
        return s.lower().replace("_", " ")

    # 1. must_include — content floor.
    must = expect["must_include"]

    for entry in must.get("goals", []):
        label_substr = _norm(entry["label_contains"])
        value_substr = entry.get("value_contains", "").lower()
        match = any(
            label_substr in _norm(g.label)
            and (not value_substr or value_substr in str(g.value).lower())
            for g in distillate.goals
        )
        assert match, (
            f"missing goal matching {entry}; got {[g.label for g in distillate.goals]}"
        )

    for entry in must.get("principles", []):
        sub = _norm(entry["label_contains"])
        assert any(sub in _norm(p.label) for p in distillate.principles), (
            f"missing principle containing {sub!r}; "
            f"got {[p.label for p in distillate.principles]}"
        )

    if "risk_priorities_first" in must:
        assert distillate.risk_priorities, "risk_priorities is empty"
        assert must["risk_priorities_first"].lower() in distillate.risk_priorities[0].lower(), (
            f"risk_priorities[0] = {distillate.risk_priorities[0]!r}; "
            f"expected to contain {must['risk_priorities_first']!r}"
        )

    if "risk_priorities_any_contains" in must:
        assert distillate.risk_priorities, "risk_priorities is empty"
        for sub in must["risk_priorities_any_contains"]:
            assert any(_norm(sub) in _norm(p) for p in distillate.risk_priorities), (
                f"no risk_priority contains {sub!r}; "
                f"got {distillate.risk_priorities}"
            )

    for sub in must.get("decision_rules_any_label_contains", []):
        assert any(_norm(sub) in _norm(r.label) for r in distillate.decision_rules), (
            f"no decision_rule label contains {sub!r}; "
            f"got {[r.label for r in distillate.decision_rules]}"
        )

    for sub in must.get("targets_labels_any_contains", []):
        assert any(_norm(sub) in _norm(t.label) for t in distillate.targets), (
            f"no target label contains {sub!r}; "
            f"got {[t.label for t in distillate.targets]}"
        )

    for sub in must.get("constraints_any_label_contains", []):
        assert any(_norm(sub) in _norm(c.label) for c in distillate.constraints), (
            f"no constraint label contains {sub!r}; "
            f"got {[c.label for c in distillate.constraints]}"
        )

    if "stress_tolerance_contains" in must:
        assert must["stress_tolerance_contains"].lower() in distillate.stress_tolerance.lower(), (
            f"stress_tolerance = {distillate.stress_tolerance!r}; "
            f"expected to contain {must['stress_tolerance_contains']!r}"
        )

    # 2. must_exclude — exclusion contract per spec §3.3.
    for forbidden in expect["must_exclude_in_serialized_text"]:
        assert forbidden.lower() not in serialized, (
            f"distillate contains forbidden time-stamped value: {forbidden!r}"
        )
