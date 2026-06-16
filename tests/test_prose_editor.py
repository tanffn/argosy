from argosy.agents.prose_editor import correct_prose_site


def test_prose_editor_passes_fact_and_returns_snippet_via_injected_editor():
    captured = {}

    def fake_editor(prompt: str) -> str:
        captured["prompt"] = prompt
        return "We keep NVDA near the 18% cap."

    out = correct_prose_site(
        fact_id="allocation.nvda_cap_pct", canonical_value=18.0,
        offending_text="We keep NVDA near the 13% cap.",
        editor=fake_editor,
    )
    assert out == "We keep NVDA near the 18% cap."
    assert "allocation.nvda_cap_pct" in captured["prompt"]
    assert "18" in captured["prompt"]
    assert "We keep NVDA near the 13% cap." in captured["prompt"]


def test_prose_editor_returns_original_on_editor_failure():
    def boom(prompt: str) -> str:
        raise RuntimeError("llm down")

    original = "We keep NVDA near the 13% cap."
    out = correct_prose_site(
        fact_id="allocation.nvda_cap_pct", canonical_value=18.0,
        offending_text=original, editor=boom,
    )
    assert out == original


def test_prose_editor_default_agent_path_is_failsafe(monkeypatch):
    # the default (agent-backed) path must fail-safe to the original snippet when
    # the LLM dispatch errors (e.g. no backend) — never raise, never blank.
    import argosy.agents.prose_editor as pe

    def boom_run_sync(self, **kw):
        raise RuntimeError("no backend")

    monkeypatch.setattr(pe.ProseEditorAgent, "run_sync", boom_run_sync)
    original = "Cap is 13%."
    out = correct_prose_site(
        fact_id="allocation.nvda_cap_pct", canonical_value=18.0,
        offending_text=original,
    )
    assert out == original
