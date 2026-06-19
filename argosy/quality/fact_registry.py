"""Canonical fact registry — steps 1-3 of the codex-converged fix for the
plan generator's non-convergence (see memory: project_canonical_fact_registry_solution).

The root failure: the synthesizer LLM hand-types numbers into 100K-char prose,
which drift from the deterministic resolver. The structural fix is to make the
LLM stop being a number source: numbers live in ONE canonical source (the
resolver), are RENDERED into placeholders, and a gate BANS any raw financial
number the LLM typed into the body. Drift then becomes impossible by
construction, and the dominant rejection class (``headline_numeric_source``)
cannot occur.

This module is the deterministic core (no LLM, no DB):

  * :func:`format_fact` / :func:`render_fact` — render a number from the resolver
    in its declared display policy (step 1).
  * :func:`render_placeholders` — substitute ``{{fact:key}}`` tokens; any
    unknown key / unresolved value is a deterministic build failure (step 2).
  * :func:`find_unauthorized_numbers` — the detect->prevent gate: any ₪ / % /
    ``age NN`` magnitude in the body that is NOT inside a ``{{fact:}}``
    placeholder is a violation (step 3).

Semantic checks ("surplus" requires margin>0, etc. — codex's step 6) are a
SEPARATE follow-on; this module only guarantees numeric single-sourcing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Kept in sync with render._pending_label() / numeric_source_gate.PENDING_LABEL
# (duplicated, not imported, so this module has no renderer/gate dependency).
PENDING_LABEL = "[derivation pending]"


# ---------------------------------------------------------------------------
# Step 1 — the registry: resolver-key -> display policy.
# ---------------------------------------------------------------------------
# The resolver (ResolvedValue) is the source of the VALUE + unit + provenance;
# this table adds the one thing it lacks: how each promotable fact is DISPLAYED.
# A key absent here cannot be rendered as a placeholder (unknown -> build fail),
# which is deliberate: only registered facts may appear in the body.
FACT_DISPLAY: dict[str, str] = {
    # Net worth / capital — headline magnitudes shown in millions.
    "portfolio.liquid_net_worth_nis": "nis_millions",
    "portfolio.net_worth_nis": "nis_millions",
    "retirement.fi_total_capital_nis": "nis_millions",
    "retirement.fi_target_nis": "nis_millions",
    "retirement.fire_bridge_nis": "nis_millions",
    "retirement.liquidity_reserve_nis": "nis_millions",
    # Signed margins / flows — shown in full so the sign + exact gap are legible.
    "retirement.fi_margin_signed_nis": "nis",
    "savings.annual_net_nis": "nis",
    "spend.fi_basis_nis": "nis",
    "spend.annual_t12_nis": "nis",
    "spend.mc_central_nis": "nis",
    # Percentages — resolver stores FRACTIONS (0-1); displayed as percent-points.
    "concentration.nvda_cap_pct": "pct",
    "concentration.nvda_current_pct": "pct",
    "retirement.required_real_yield_pct": "pct",
    "retirement.return_assumption_pct": "pct",
    # Ages.
    "retirement.fi_age": "age",
    "retirement.earliest_safe_age": "age",
    # FX.
    "fx.usd_nis": "fx",
}


def format_fact(value: float, unit: str, *, display: str) -> str:
    """Render ``value`` in its display policy. Matches the existing renderer's
    forms exactly (render._n / _fmt_nis_m / pct / age) so a placeholder-rendered
    fact is byte-identical to what the resolver-bound surfaces already show."""
    v = float(value)
    if display == "nis":
        return f"₪{v:,.0f}"
    if display == "nis_millions":
        return f"₪{v / 1e6:.2f}M"
    if display == "pct":
        # resolver stores fractions; show percent-points
        return f"{v * 100:.1f}%"
    if display == "age":
        return f"age {v:.0f}"
    if display == "fx":
        return f"{v:.3f}"
    raise PlaceholderError(f"unknown display policy {display!r}")


class PlaceholderError(ValueError):
    """A placeholder could not be rendered — unknown key, unregistered key, or an
    unresolved/pending value. This is a DETERMINISTIC BUILD FAILURE: the body must
    never ship with an unresolved fact (that is exactly the drift we prevent)."""


def render_fact(key: str, resolved, *, registry: dict[str, str] = FACT_DISPLAY) -> str:
    """Render one registered fact from the resolver. Raises PlaceholderError when
    the key is not registered or its resolved value is missing/pending/None."""
    display = registry.get(key)
    if display is None:
        raise PlaceholderError(f"fact key not in registry: {key!r}")
    rv = resolved.get(key)
    if rv is None or getattr(rv, "status", None) != "resolved" or getattr(rv, "value", None) is None:
        raise PlaceholderError(
            f"fact {key!r} is not resolved (status="
            f"{getattr(rv, 'status', 'MISSING')!r}) — cannot render"
        )
    return format_fact(rv.value, getattr(rv, "unit", ""), display=display)


# ---------------------------------------------------------------------------
# Step 2 — placeholder substitution.
# ---------------------------------------------------------------------------
_PLACEHOLDER = re.compile(r"\{\{fact:([A-Za-z0-9_.]+)\}\}")


def render_placeholders(
    text: str, resolved, *, registry: dict[str, str] = FACT_DISPLAY, strict: bool = True
) -> str:
    """Replace every ``{{fact:key}}`` with the canonical rendered value.

    ``strict=True`` (default): an unknown/unregistered key or unresolved value
    raises PlaceholderError — the build fails rather than shipping a hole or a
    stale number. ``strict=False``: leave an unrenderable token in place (so the
    ban-gate / numeric gate surfaces it) instead of aborting; used by the
    best-effort assembly wiring before strict enforcement is switched on."""
    def _sub(m: re.Match) -> str:
        try:
            return render_fact(m.group(1), resolved, registry=registry)
        except PlaceholderError:
            if strict:
                raise
            return m.group(0)

    return _PLACEHOLDER.sub(_sub, text or "")


# ---------------------------------------------------------------------------
# Step 3 — the detect->prevent gate: ban raw financial numbers in the body.
# ---------------------------------------------------------------------------
# Only FINANCIAL MAGNITUDES are policed (money / percent / explicit "age NN") —
# NOT bare integers, years, dates, section numbers or counts (codex's caution:
# a blunt "every number" scan breaks units/ages/counts). A magnitude that is not
# inside a {{fact:}} placeholder is the LLM acting as a number source -> ban it.
_NIS_TOKEN = re.compile(r"₪\s*-?\s*\d[\d,]*(?:\.\d+)?\s*[MmKk]?")
_PCT_TOKEN = re.compile(r"\d+(?:\.\d+)?\s*%")
_AGE_TOKEN = re.compile(r"\bage[\s-]*\d{2}\b", re.IGNORECASE)


@dataclass(frozen=True)
class NumberViolation:
    token: str
    kind: str  # "nis" | "pct" | "age"
    pos: int


def find_unauthorized_numbers(text: str) -> list[NumberViolation]:
    """Return every raw financial magnitude (₪ / % / ``age NN``) in ``text`` that
    is NOT inside a ``{{fact:}}`` placeholder. An empty list means the body is
    single-sourced (all magnitudes are placeholders) and may proceed to render."""
    if not text:
        return []
    # Blank out placeholder spans (preserve length so positions stay meaningful)
    # so magnitudes that legitimately live inside a placeholder key aren't scanned.
    masked = _PLACEHOLDER.sub(lambda m: " " * len(m.group(0)), text)
    # The pending escape hatch carries no magnitude; nothing to mask there.
    out: list[NumberViolation] = []
    for kind, pat in (("nis", _NIS_TOKEN), ("pct", _PCT_TOKEN), ("age", _AGE_TOKEN)):
        for m in pat.finditer(masked):
            out.append(NumberViolation(token=m.group(0).strip(), kind=kind, pos=m.start()))
    out.sort(key=lambda v: v.pos)
    return out
