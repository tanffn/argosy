"""Deterministic leakage gate: an assembled plan artifact must never ship with
unrendered placeholders or leaked emission scaffolding. The whole-artifact reader
(an LLM coherence critic) does NOT reliably catch these, so a leaky draft falsely
read as promotable — this gate is the engineered, fail-closed backstop."""
from __future__ import annotations

from argosy.quality.leakage_gate import (
    LEAKAGE_PATTERNS,
    scan_leakage,
    is_leak_clean,
)


def test_clean_text_has_no_leakage():
    assert scan_leakage("The plan is fully rendered: liquid ₪11.67M, retire at age 46.") == []
    assert is_leak_clean("Nothing leaked here.")


def test_detects_derivation_pending():
    hits = scan_leakage("Perpetuity base [derivation pending] NIS short of target.")
    assert any("derivation pending" in h.lower() for h in hits)
    assert not is_leak_clean("Perpetuity base [derivation pending] NIS.")


def test_detects_emit_as_instruction_leak():
    hits = scan_leakage("...from fi_methodology.swr_real_pct. EMIT AS [derivation pending].")
    # both the EMIT AS scaffolding and the pending token are leaks
    assert any("EMIT AS" in h for h in hits)
    assert not is_leak_clean("foo EMIT AS bar")


def test_detects_unrendered_fact_placeholder():
    hits = scan_leakage("Net worth is {{fact:portfolio.net_worth_nis}} today.")
    assert any("{{fact:" in h for h in hits)
    assert not is_leak_clean("see {{fact:retirement.fi_age}}")


def test_scan_returns_one_entry_per_distinct_pattern_with_count():
    text = "a [derivation pending] b [derivation pending] c EMIT AS d {{fact:x}}"
    hits = scan_leakage(text)
    # de-duplicated by pattern (not one per occurrence), so the report is compact
    assert len(hits) == 3
    # the derivation-pending entry reflects the 2 occurrences
    assert any("derivation pending" in h.lower() and "2" in h for h in hits)


def test_patterns_are_the_three_known_leak_classes():
    assert set(LEAKAGE_PATTERNS) == {"[derivation pending]", "EMIT AS", "{{fact:"}
