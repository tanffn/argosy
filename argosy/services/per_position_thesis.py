"""Per-position thesis derivation (T4.1).

Pure-Python transformer — no LLM, no new agent class. Reads:

  * The pending draft's horizon JSONs (``horizon_short_json`` /
    ``horizon_medium_json`` / ``horizon_long_json``) to learn which
    tickers the plan wants to grow, trim, or reshape.
  * The current portfolio snapshot (positions + USD values) so we can
    compute "current weight" + "delta from target weight".
  * The synthesis run's ``agent_reports`` rows (response_text +
    sources_json) so we can attribute conviction + cited sources to
    each ticker.

Emits a list of :class:`PositionThesis` cards:

  * One per held ticker with a verdict ``HOLD|BUY|TRIM|SELL`` derived
    from horizon targets/actions.
  * One per "should add" ticker (verdict ``ADD``) — tickers the plan
    mentions in its action labels/details but which aren't in the user's
    portfolio today (UCITS replacements like ``XEON`` / ``ERNA`` /
    ``CSPX`` show up here).

The plan refers to this module as ``argosy/agents/per_position_thesis.py``
but it's not an agent — no LLM is invoked. Living under
``argosy/services/`` matches the project convention for pure derivation
helpers (cf. ``portfolio_snapshot_store.py``, ``agent_tree_builder.py``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Tunables — kept module-level so the test suite can verify the exact
# thresholds via inspection rather than mocking. The values are tuned
# against the May 2026 NVDA/SCHD/SGOV draft and documented in the T4.1
# return summary so future revisions can argue from data.
_TRIM_VS_SELL_RATIO = 0.50
"""When a target wants the position weight reduced by more than this
fraction of the *current* weight, classify as ``SELL`` rather than
``TRIM``. Example: NVDA at 64.9% with a target of 45% loses ~31% of its
current weight => still ``TRIM``; a target of 15% loses ~77% =>
``SELL``."""

_REASONING_CAP_CHARS = 500
"""Hard cap on the reasoning_md field per card. Plenty of room for 2-3
sentences; UI doesn't need a wall of text."""


@dataclass
class PositionThesis:
    """One per-position card.

    All fields are JSON-serializable scalars or simple lists so the
    route layer can pass them straight through pydantic without an
    extra round of model declarations.
    """

    ticker: str
    current_shares: float | None
    current_weight_pct: float | None
    current_usd_value: float | None
    verdict: str  # HOLD | BUY | TRIM | SELL | ADD
    conviction: str  # HIGH | MEDIUM | LOW
    reasoning_md: str
    cited_sources: list[str] = field(default_factory=list)
    target_weight_pct: float | None = None
    target_shares: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# A "ticker" mention has to match a stand-alone uppercase token so we
# don't match "AS" inside "AS WE NOTED" etc. This is the same trick the
# concentration agent uses for ticker-extraction.
_TICKER_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9.\-]{0,9})(?![A-Z0-9])")


def _mentions(text: str, ticker: str) -> bool:
    """True iff ``ticker`` appears as a stand-alone token in ``text``.

    Case-insensitive but anchored on word boundaries so ``SGOV`` doesn't
    match against ``MSGOVT``. Both the ticker and the text are upper-
    cased before the substring check.
    """
    if not text or not ticker:
        return False
    upper = text.upper()
    tk = ticker.upper()
    # Quick reject — if the ticker isn't even a substring, skip the regex.
    if tk not in upper:
        return False
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(tk)}(?![A-Z0-9])")
    return bool(pattern.search(upper))


def _extract_candidate_tickers(text: str) -> set[str]:
    """Pull plausible ticker symbols out of free-form text.

    Filters out a small stop-list of all-caps English words that aren't
    tickers (US, IT, OK, NO, OR, BE, ...). Captures 1-10 char uppercase
    tokens; UCITS tickers like ``CSPX``, ``XEON``, ``FWRA`` all fit.
    """
    if not text:
        return set()
    tokens = _TICKER_RE.findall(text)
    return {t for t in tokens if t.upper() not in _STOP_WORDS}


# Common all-caps words that match the ticker regex but aren't tickers.
# Tuned against the actual horizon JSONs in db/argosy.db so we don't
# strip out real tickers.
_STOP_WORDS: frozenset[str] = frozenset({
    "A", "AN", "AND", "OR", "BUT", "IF", "AS", "AT", "BY", "FOR",
    "FROM", "IN", "INTO", "ON", "OF", "OUT", "TO", "UP", "WITH",
    "BE", "DO", "IS", "IT", "NO", "NOT", "OK", "SO", "US", "VS",
    "WE", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
    "GMT", "EST", "PST", "PT", "ET", "AM", "PM", "Q1", "Q2", "Q3", "Q4",
    "USD", "EUR", "NIS", "GBP", "ILS", "JPY", "CHF", "CAD", "AUD",
    "FX", "ETF", "RSU", "PE", "PB", "EPS", "P", "E", "B", "PT",
    "RED", "AMBER", "YELLOW", "GREEN", "HIGH", "MEDIUM", "LOW",
    "ESG", "AML", "KYC", "API", "JSON", "TSV", "CSV", "URL",
    "ASAP", "TBD", "TODO", "FYI",
    "SDD", "WTI", "PCE", "CPI", "GDP", "VIX",
    "POA", "IRA", "401K", "529",
    "ON", "NEXT", "PRIOR", "LAST", "ALL", "ANY", "NEW",
    "BUY", "SELL", "HOLD", "TRIM", "ADD",  # verdict words
    "MA", "MACD", "RSI", "ATR",  # technical indicator names
    "MSCI", "SPDR",  # index brand prefixes that AREN'T themselves tickers
    "RISK", "OFF", "TACTICAL", "STRATEGIC",
})


def _scan_horizon_for_ticker(
    horizon_payload: dict[str, Any],
    ticker: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return (matching_targets, matching_actions, rationale_snippets).

    A target/action matches the ticker when its label OR detail OR
    rationale field contains the ticker as a word boundary token.
    """
    matched_targets: list[dict[str, Any]] = []
    matched_actions: list[dict[str, Any]] = []
    rationale_snippets: list[str] = []

    for t in horizon_payload.get("targets") or []:
        if not isinstance(t, dict):
            continue
        blob = " ".join(
            str(t.get(k, ""))
            for k in ("label", "rationale", "source_section")
        )
        if _mentions(blob, ticker):
            matched_targets.append(t)
            if t.get("rationale"):
                rationale_snippets.append(str(t["rationale"]))

    for a in horizon_payload.get("actions") or []:
        if not isinstance(a, dict):
            continue
        blob = " ".join(
            str(a.get(k, ""))
            for k in ("label", "detail", "rationale")
        )
        if _mentions(blob, ticker):
            matched_actions.append(a)
            if a.get("rationale"):
                rationale_snippets.append(str(a["rationale"]))

    return matched_targets, matched_actions, rationale_snippets


def _classify_verdict(
    ticker: str,
    current_weight_pct: float | None,
    matched_targets: list[dict[str, Any]],
    matched_actions: list[dict[str, Any]],
) -> tuple[str, float | None, int | None]:
    """Decide HOLD / BUY / TRIM / SELL for one held ticker.

    Returns ``(verdict, target_weight_pct, target_shares)`` — the
    target columns are pulled from the strongest matching target if
    one exists so the UI can show "12-month target: 45%".

    Heuristic:

      * Look at targets first. A target whose label mentions "share"
        + "ceiling" or "share count" => translate value to
        ``target_shares``. A target whose unit is "pct_of_portfolio"
        or whose label mentions "share of portfolio" / "weight" =>
        ``target_weight_pct``.
      * If we have a numeric ``target_weight_pct`` and a
        ``current_weight_pct``:
            - target >= current * 1.10 => BUY (plan wants more)
            - target <= current * (1 - _TRIM_VS_SELL_RATIO) => SELL
            - target <  current * 0.95 => TRIM
            - otherwise => HOLD
      * No numeric target? Look at action labels for words like
        "sell", "trim", "liquidate", "buy", "add", "increase",
        "reduce", "deconcentrate". Each maps to the obvious verdict;
        ties resolve toward TRIM (the more conservative move).
      * Falls through to HOLD when nothing matched.
    """
    target_weight_pct: float | None = None
    target_shares: int | None = None

    # Pull numeric target hints from matched targets.
    for t in matched_targets:
        unit = (t.get("unit") or "").lower()
        label = (t.get("label") or "").lower()
        value = t.get("value")
        if not isinstance(value, (int, float)):
            continue
        if (
            unit == "pct_of_portfolio"
            or "share of portfolio" in label
            or "% of portfolio" in label
            or "weight" in label and "ratio" not in label
        ):
            # Take the first / strongest one.
            if target_weight_pct is None:
                target_weight_pct = float(value)
        elif unit == "shares" or "share count" in label or "share ceiling" in label:
            if target_shares is None:
                target_shares = int(value)

    # Numeric path — weight comparison.
    if (
        target_weight_pct is not None
        and current_weight_pct is not None
        and current_weight_pct > 0
    ):
        # ratio of target to current.
        delta = target_weight_pct - current_weight_pct
        rel = delta / current_weight_pct
        if rel >= 0.10:
            return ("BUY", target_weight_pct, target_shares)
        if rel <= -_TRIM_VS_SELL_RATIO:
            return ("SELL", target_weight_pct, target_shares)
        if rel <= -0.05:
            return ("TRIM", target_weight_pct, target_shares)
        return ("HOLD", target_weight_pct, target_shares)

    # Heuristic path — look at action labels for verb cues.
    label_blob = " ".join(
        (a.get("label") or "") + " " + (a.get("detail") or "")
        for a in matched_actions
    ).lower()

    # Strong sell signals: "liquidate", "exit", "sell all", "close position".
    sell_cues = ("liquidate", "exit", "sell all", "close position", "close out")
    if any(cue in label_blob for cue in sell_cues):
        return ("SELL", target_weight_pct, target_shares)

    # Trim signals: "deconcentrate", "reduce", "trim", "sell", "sale", "down to".
    trim_cues = (
        "deconcentrat", "reduce", "trim", " sell ", "sale", "down to",
        "tighten", "tighter", "scale back",
    )
    if any(cue in label_blob for cue in trim_cues):
        return ("TRIM", target_weight_pct, target_shares)

    # Buy / add cues.
    buy_cues = ("redeploy", "add ", "buy ", "accumulate", "increase", "grow")
    if any(cue in label_blob for cue in buy_cues):
        return ("BUY", target_weight_pct, target_shares)

    return ("HOLD", target_weight_pct, target_shares)


def _aggregate_conviction(
    analyst_reports: Iterable[dict[str, Any]],
    ticker: str,
) -> str:
    """Majority-vote analyst confidence for one ticker.

    For each analyst row whose ``response_text`` mentions the ticker,
    contribute its confidence (``HIGH`` / ``MEDIUM`` / ``LOW``). Then:

      * If HIGH outnumbers everything else => HIGH.
      * Else if LOW is strictly dominant => LOW.
      * Else MEDIUM (the default for "no data" or "mixed").

    Tickers with zero analyst mentions also resolve to LOW since we have
    no evidence to back any verdict — the UI surfaces that as the "we're
    flying blind on this one" signal.
    """
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    matched_any = False
    for row in analyst_reports:
        text = row.get("response_text") or ""
        if not _mentions(text, ticker):
            continue
        matched_any = True
        conf = (row.get("confidence") or "").upper()
        if conf in counts:
            counts[conf] += 1
        else:
            # Unknown / NULL confidence — bucket as MEDIUM (the default).
            counts["MEDIUM"] += 1
    if not matched_any:
        return "LOW"
    high, medium, low = counts["HIGH"], counts["MEDIUM"], counts["LOW"]
    if high > medium + low:
        return "HIGH"
    if low > high + medium:
        return "LOW"
    return "MEDIUM"


def _collect_cited_sources(
    analyst_reports: Iterable[dict[str, Any]],
    ticker: str,
) -> list[str]:
    """Return source_ids from analyst sources_json rows that mention the ticker.

    Each ``sources_json`` row is a list of ``{source_id, content}``
    dicts. We accept the source if either the ``source_id`` itself
    or the ``content`` body mentions the ticker as a token (case-
    insensitive; ``source_id`` like ``indicators/NVDA`` matches the
    substring path).
    """
    out: list[str] = []
    seen: set[str] = set()
    for row in analyst_reports:
        raw = row.get("sources_json")
        if not raw:
            continue
        try:
            sources = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(sources, list):
            continue
        for s in sources:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("source_id") or "")
            content = str(s.get("content") or "")
            # ``source_id`` uses ``/`` segments so a plain ".upper() in" check
            # is OK; for ``content`` we use the word-boundary _mentions guard.
            if not sid:
                continue
            if (
                ticker.upper() in sid.upper()
                or _mentions(content, ticker)
            ):
                if sid not in seen:
                    seen.add(sid)
                    out.append(sid)
    return out


def _assemble_reasoning(
    ticker: str,
    rationale_snippets: list[str],
    analyst_reports: Iterable[dict[str, Any]],
) -> str:
    """Pick the strongest 2-3 snippets and join into markdown.

    Strategy: take horizon rationale strings first (they're already
    distilled), then a short excerpt (~120 chars) from each analyst row
    that mentions the ticker. Dedupe trivial duplicates. Hard-cap at
    ``_REASONING_CAP_CHARS`` so the UI doesn't overflow.
    """
    pieces: list[str] = []
    seen_prefixes: set[str] = set()

    def _add(text: str) -> None:
        s = (text or "").strip()
        if not s:
            return
        prefix = s[:60].lower()
        if prefix in seen_prefixes:
            return
        seen_prefixes.add(prefix)
        pieces.append(s)

    for snippet in rationale_snippets:
        _add(snippet)

    for row in analyst_reports:
        text = row.get("response_text") or ""
        if not _mentions(text, ticker):
            continue
        # Pull a short window around the first ticker mention so the
        # excerpt is contextual.
        upper = text.upper()
        idx = upper.find(ticker.upper())
        if idx < 0:
            continue
        start = max(0, idx - 40)
        end = min(len(text), idx + 200)
        excerpt = text[start:end].strip()
        if excerpt:
            # Map internal agent_role to a user-friendly label so the UI
            # doesn't see "(fundamentals_analyst)" verbatim. See
            # argosy/services/plain_english_labels.py.
            from argosy.services.plain_english_labels import friendly_agent_role
            role = friendly_agent_role(row.get("agent_role"))
            _add(f"({role}) … {excerpt} …")

    blob = "\n\n".join(pieces[:4])
    if len(blob) > _REASONING_CAP_CHARS:
        # Cut at the last whitespace before the cap so we don't break a word.
        cut = blob.rfind(" ", 0, _REASONING_CAP_CHARS - 1)
        if cut < 0:
            cut = _REASONING_CAP_CHARS - 1
        blob = blob[:cut].rstrip() + "…"
    return blob


def _ticker_to_position(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group positions by ticker, summing across accounts.

    The portfolio TSV has multiple rows for the same symbol (e.g. NVDA
    at Schwab and NVDA at Leumi); we aggregate so the card shows one
    consolidated holding per ticker. Cash / real-estate rows with no
    symbol are skipped.
    """
    out: dict[str, dict[str, Any]] = {}
    for p in positions:
        sym = (p.get("symbol") or "").strip().upper()
        if not sym:
            continue
        shares = p.get("shares")
        usd_value_k = p.get("usd_value_k")
        rec = out.setdefault(sym, {"ticker": sym, "shares": 0.0, "usd_value_k": 0.0})
        if isinstance(shares, (int, float)):
            rec["shares"] += float(shares)
        if isinstance(usd_value_k, (int, float)):
            rec["usd_value_k"] += float(usd_value_k)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_position_theses(
    plan_version: Any,
    portfolio_snapshot: Any,
    agent_reports: list[Any] | list[dict[str, Any]],
) -> list[PositionThesis]:
    """Derive one thesis card per held ticker, plus "should add" cards.

    Args:
        plan_version: An object with ``horizon_short_json``,
            ``horizon_medium_json``, ``horizon_long_json`` attributes —
            typically a ``PlanVersion`` ORM row. Mapping/dict input is
            also accepted (useful for tests).
        portfolio_snapshot: Object with a ``positions`` attribute (or
            ``["positions"]`` key) — typically a ``PortfolioSnapshot``
            pydantic model. Positions must expose ``symbol`` /
            ``shares`` / ``usd_value_k``.
        agent_reports: Iterable of ``AgentReport`` ORM rows or dicts.
            Each must expose ``response_text``, ``confidence``,
            ``agent_role``, and ``sources_json`` (string or already-
            parsed list).

    Returns:
        list[PositionThesis] sorted so current holdings come first
        (by ``current_usd_value`` desc), with "should add" cards
        appended at the end.
    """

    # ---- Normalize inputs --------------------------------------------------
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    horizon_payloads: dict[str, dict[str, Any]] = {}
    for h_key, json_attr in (
        ("short", "horizon_short_json"),
        ("medium", "horizon_medium_json"),
        ("long", "horizon_long_json"),
    ):
        raw = _get(plan_version, json_attr)
        if not raw:
            continue
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            logger.warning("plan_version.%s is not valid JSON; skipping", json_attr)
            continue
        if isinstance(payload, dict):
            horizon_payloads[h_key] = payload

    # Portfolio positions — accept either a pydantic snapshot, a dict, or
    # a list of position dicts directly.
    raw_positions = _get(portfolio_snapshot, "positions", portfolio_snapshot)
    positions_norm: list[dict[str, Any]] = []
    if isinstance(raw_positions, list):
        for p in raw_positions:
            if isinstance(p, dict):
                positions_norm.append(p)
            else:
                # pydantic model — pull the attrs we need.
                positions_norm.append({
                    "symbol": getattr(p, "symbol", "") or "",
                    "shares": getattr(p, "shares", None),
                    "usd_value_k": getattr(p, "usd_value_k", None),
                })

    held_map = _ticker_to_position(positions_norm)

    total_usd_value_k = sum(
        rec.get("usd_value_k") or 0.0 for rec in held_map.values()
    )
    if total_usd_value_k <= 0:
        # Fall back to the snapshot's reported total if available so
        # current_weight_pct is still meaningful even when individual
        # rows had missing values.
        total_usd_value_k = float(
            _get(portfolio_snapshot, "total_usd_value_k", 0.0) or 0.0
        )

    # Analyst reports — normalize ORM rows into dicts so the helpers
    # don't have to do ``getattr`` everywhere.
    analyst_norm: list[dict[str, Any]] = []
    for row in agent_reports or []:
        if isinstance(row, dict):
            analyst_norm.append(row)
        else:
            analyst_norm.append({
                "agent_role": getattr(row, "agent_role", "") or "",
                "response_text": getattr(row, "response_text", "") or "",
                "confidence": getattr(row, "confidence", None),
                "sources_json": getattr(row, "sources_json", None),
            })

    # ---- Held tickers ------------------------------------------------------
    held_cards: list[PositionThesis] = []
    for ticker, rec in held_map.items():
        usd_value = float(rec.get("usd_value_k") or 0.0) * 1000.0
        current_weight_pct: float | None = None
        if total_usd_value_k > 0:
            current_weight_pct = (
                (rec.get("usd_value_k") or 0.0) / total_usd_value_k * 100.0
            )

        # Aggregate across all three horizons (long carries strategic
        # ceilings, medium carries tactical tranches, short carries
        # immediate actions — all are valid signals).
        all_targets: list[dict[str, Any]] = []
        all_actions: list[dict[str, Any]] = []
        all_snippets: list[str] = []
        for h_payload in horizon_payloads.values():
            ts, acs, snips = _scan_horizon_for_ticker(h_payload, ticker)
            all_targets.extend(ts)
            all_actions.extend(acs)
            all_snippets.extend(snips)

        verdict, tgt_w, tgt_sh = _classify_verdict(
            ticker, current_weight_pct, all_targets, all_actions
        )
        conviction = _aggregate_conviction(analyst_norm, ticker)
        cited = _collect_cited_sources(analyst_norm, ticker)
        reasoning = _assemble_reasoning(ticker, all_snippets, analyst_norm)

        held_cards.append(PositionThesis(
            ticker=ticker,
            current_shares=float(rec.get("shares") or 0.0) or None,
            current_weight_pct=(
                round(current_weight_pct, 2)
                if current_weight_pct is not None else None
            ),
            current_usd_value=round(usd_value, 2) if usd_value else None,
            verdict=verdict,
            conviction=conviction,
            reasoning_md=reasoning,
            cited_sources=cited,
            target_weight_pct=tgt_w,
            target_shares=tgt_sh,
        ))

    # Sort held cards by USD value descending so the user sees the
    # biggest positions first.
    held_cards.sort(
        key=lambda c: (c.current_usd_value or 0.0),
        reverse=True,
    )

    # ---- "Should add" tickers ---------------------------------------------
    held_set = set(held_map.keys())
    candidate_tickers: set[str] = set()
    for h_payload in horizon_payloads.values():
        for a in h_payload.get("actions") or []:
            if not isinstance(a, dict):
                continue
            text = " ".join(
                str(a.get(k, "")) for k in ("label", "detail", "rationale")
            )
            candidate_tickers |= _extract_candidate_tickers(text)
        for t in h_payload.get("targets") or []:
            if not isinstance(t, dict):
                continue
            text = " ".join(
                str(t.get(k, "")) for k in ("label", "rationale")
            )
            candidate_tickers |= _extract_candidate_tickers(text)

    add_candidates = sorted(candidate_tickers - held_set)
    add_cards: list[PositionThesis] = []
    for ticker in add_candidates:
        all_targets: list[dict[str, Any]] = []
        all_actions: list[dict[str, Any]] = []
        all_snippets: list[str] = []
        for h_payload in horizon_payloads.values():
            ts, acs, snips = _scan_horizon_for_ticker(h_payload, ticker)
            all_targets.extend(ts)
            all_actions.extend(acs)
            all_snippets.extend(snips)
        # Skip tickers that only appeared inside a stop-word-flanked
        # rationale — i.e., no real action targeted them.
        if not all_actions and not all_targets:
            continue
        conviction = _aggregate_conviction(analyst_norm, ticker)
        cited = _collect_cited_sources(analyst_norm, ticker)
        reasoning = _assemble_reasoning(ticker, all_snippets, analyst_norm)
        add_cards.append(PositionThesis(
            ticker=ticker,
            current_shares=None,
            current_weight_pct=None,
            current_usd_value=None,
            verdict="ADD",
            conviction=conviction,
            reasoning_md=reasoning,
            cited_sources=cited,
            target_weight_pct=None,
            target_shares=None,
        ))

    return held_cards + add_cards


def emit_thesis_predictions(
    session: "Any",
    user_id: str,
    *,
    plan_version_id: int | None,
    theses: list[PositionThesis],
    event_at: "datetime | None" = None,
) -> None:
    """Spec C commit #3 — fan-out one prediction row per thesis card.

    Per spec §2.4 / codex BLOCKER #3 (anti-hide-behind-HOLD), every
    thesis card is logged as a prediction including HOLDs (HOLD →
    direction='neutral', still scored against subsequent price action).
    Caller passes the session + plan_version_id (used as thesis_id for
    dedup); ``derive_position_theses`` doesn't take a session today, so
    the call-site (route handler / synthesis driver) invokes both:

        theses = derive_position_theses(...)
        emit_thesis_predictions(session, user_id, plan_version_id=pv.id, theses=theses)

    Best-effort: any per-card failure is logged + swallowed so a writer
    issue never breaks the thesis-derivation primary path.

    Args:
      session: live SQLAlchemy Session. Caller owns the outer transaction;
        this function commits inline so per-card failures don't lose
        prior writes.
      user_id: tenant id (FK to users.id).
      plan_version_id: the PlanVersion row's id; used as the stable
        ``thesis_id`` component of the dedup key
        ``v1|predictions|thesis|<plan_version_id>.<ticker>``. ``None``
        skips the emit entirely (no stable id ⇒ no dedup-safe write).
      theses: the cards returned by ``derive_position_theses``.
      event_at: when the synthesis run produced these cards. Defaults to
        wallclock UTC ``now()`` if missing — the call-site SHOULD pass
        the synthesis run's completion timestamp for correct entry-price
        anchoring per spec §2.3.
    """
    if plan_version_id is None:
        return
    try:
        from datetime import datetime as _dt, timezone as _tz
        from argosy.services.predictions.writers import (
            write_per_position_thesis_prediction,
        )
    except Exception:  # pragma: no cover — import-guard
        logger.exception("emit_thesis_predictions: import failed")
        return

    when = event_at if event_at is not None else _dt.now(_tz.utc)
    for card in theses:
        try:
            # The thesis-derivation verdict "ADD" maps to BUY at write
            # time — ADD is the "should-add" variant of BUY and the
            # writer's action enum doesn't have a separate ADD entry.
            # Translate at the call-site so the writer's enum stays
            # closed.
            action = card.verdict
            if action == "ADD":
                action = "BUY"
            if action not in ("BUY", "TRIM", "SELL", "HOLD"):
                # Unknown verdict (defensive against future cards) —
                # skip rather than mis-classify into the ledger.
                continue
            # Note: target_weight_pct on a PositionThesis is a
            # PORTFOLIO ALLOCATION weight (e.g. "NVDA should be 45% of
            # portfolio"), NOT a ticker price target. The predictions
            # ledger's target_price column is a price level; the two
            # don't map directly. We leave target_price=None here so
            # the writer picks fixed_lookahead_30d (the correct method
            # for direction-only predictions per spec §3.1).
            # SAVEPOINT-wrapped so a writer FK / CHECK failure rolls
            # back ONLY this card, never the outer caller's session.
            with session.begin_nested():
                write_per_position_thesis_prediction(
                    session,
                    user_id,
                    thesis_id=plan_version_id,
                    ticker=card.ticker,
                    action=action,  # type: ignore[arg-type]
                    conviction=card.conviction,  # type: ignore[arg-type]
                    event_at=when,
                    target_price=None,
                    stop_price=None,
                )
            session.commit()
        except Exception:  # noqa: BLE001 — never break the batch
            logger.exception(
                "emit_thesis_predictions: write failed for ticker=%s",
                card.ticker,
            )


__all__ = ["PositionThesis", "derive_position_theses", "emit_thesis_predictions"]
