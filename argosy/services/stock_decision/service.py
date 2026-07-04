"""Orchestration for per-stock research -> verdict.

``research_bundle`` assembles the fetched data for one ticker from a set of
best-effort fetchers (news / fundamentals / sentiment / technical / price / plan
thesis). Each fetcher is injected and isolated: a failure or empty result simply
leaves that field ABSENT, and the decision agent lowers its confidence + records
the gap rather than guessing.

``decide_holdings`` runs the review over a book: a cheap ``triage`` gate picks
which names warrant deep research (tiering — a full fetch+decide on every ticker
is what timed the fleet out), then each survivor gets a bundle + a verdict. HOLD
verdicts are returned but are NOT actionable — the caller surfaces only
``actionable_verdicts`` so an intact thesis stays silent.

All collaborators are injectable so the orchestration is unit-tested without a
live LLM or network; the live wiring (real analyst fetchers, inbox sink) is a thin
adapter over this core.
"""
from __future__ import annotations

from typing import Any, Callable

from argosy.agents.stock_decision import (
    StockDecisionOutput,
    decide_stock,
    is_actionable,
)

# A fetcher maps a ticker -> a short text summary (or None when unavailable).
Fetcher = Callable[[str], "str | None"]


def research_bundle(ticker: str, *, fetchers: dict[str, Fetcher]) -> dict[str, Any]:
    """Assemble the fetched research bundle for ``ticker``. Best-effort per field:
    a fetcher that returns falsy or raises leaves its field out of the bundle."""
    bundle: dict[str, Any] = {}
    for field, fn in (fetchers or {}).items():
        try:
            value = fn(ticker)
        except Exception:  # noqa: BLE001 — one bad source must not blank the bundle
            value = None
        if value:
            bundle[field] = value
    return bundle


def decide_holdings(
    holdings: dict[str, float],
    *,
    fetchers: dict[str, Fetcher],
    context_of: Callable[[str, float], str],
    triage: Callable[[str, float], bool] | None = None,
    decide: Callable[..., StockDecisionOutput] = decide_stock,
    user_id: str = "ariel",
) -> list[StockDecisionOutput]:
    """Review a book (``ticker -> usd``): triage to the material names, fetch a
    bundle for each, and reach a verdict. Returns ALL verdicts (incl. HOLD) — the
    caller filters with ``actionable_verdicts`` so HOLDs stay silent.

    ``triage`` is the tiering gate: return False to skip a name's (expensive) deep
    research. ``None`` researches every holding.
    """
    verdicts: list[StockDecisionOutput] = []
    for ticker, usd in (holdings or {}).items():
        if triage is not None and not triage(ticker, float(usd)):
            continue
        bundle = research_bundle(ticker, fetchers=fetchers)
        verdicts.append(
            decide(ticker, context=context_of(ticker, float(usd)),
                   bundle=bundle, user_id=user_id)
        )
    return verdicts


def actionable_verdicts(
    verdicts: list[StockDecisionOutput],
) -> list[StockDecisionOutput]:
    """Only the verdicts that need the client (BUY/SELL/TRIM). HOLD is dropped —
    thesis intact, no action, so it never becomes inbox noise."""
    return [v for v in verdicts if is_actionable(v.verdict)]


__all__ = ["research_bundle", "decide_holdings", "actionable_verdicts", "Fetcher"]
