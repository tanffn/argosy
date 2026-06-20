"""The synthesizer guidance presents each headline figure as
``→ EMIT AS: {{fact:KEY}}``; the model sometimes copies that SCAFFOLDING into the
prose body (observed: 26 leaks in a live draft, e.g. "...swr_real_pct. EMIT AS
[derivation pending]."). ``strip_emission_scaffolding`` deterministically removes the
leaked scaffolding at render — keeping the {{fact:}} token (so its canonical value
still renders) but dropping the "EMIT AS" verb and any leaked pending stub."""
from __future__ import annotations

from argosy.quality.fact_registry import (
    render_placeholders,
    strip_emission_scaffolding,
)


class _RV:
    def __init__(self, value, unit, status="resolved"):
        self.value, self.unit, self.status = value, unit, status


class _Resolved:
    def __init__(self, d):
        self._d = d

    def get(self, k):
        return self._d.get(k)


def test_strips_emit_as_prefix_keeping_the_fact_token():
    out = strip_emission_scaffolding(
        "Net worth is EMIT AS: {{fact:portfolio.net_worth_nis}} today."
    )
    assert "EMIT AS" not in out
    assert "{{fact:portfolio.net_worth_nis}}" in out  # token preserved → value still renders


def test_strips_leaked_pending_stub_entirely():
    out = strip_emission_scaffolding(
        "The SWR is sized from fi_methodology.swr_real_pct. EMIT AS [derivation pending]."
    )
    assert "EMIT AS" not in out
    assert "[derivation pending]" not in out
    assert "sized from fi_methodology.swr_real_pct." in out


def test_strips_arrow_emit_as_form():
    out = strip_emission_scaffolding("Perpetuity base → EMIT AS: {{fact:retirement.fi_target_nis}}")
    assert "EMIT AS" not in out and "→" not in out
    assert "{{fact:retirement.fi_target_nis}}" in out


def test_leaves_ordinary_prose_untouched():
    s = "Retire at age 46; NVDA is 62.5% of the book."
    assert strip_emission_scaffolding(s) == s


def test_render_placeholders_sanitizes_then_substitutes():
    resolved = _Resolved({"portfolio.net_worth_nis": _RV(11_870_000.0, "nis")})
    out = render_placeholders(
        "Net worth EMIT AS: {{fact:portfolio.net_worth_nis}}.", resolved, strict=False
    )
    assert "EMIT AS" not in out
    assert "{{fact:" not in out          # token rendered
    assert "11.87M" in out               # canonical value present
