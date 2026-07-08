"""FM first-greeting assembly — the data behind ``GET /api/home/greeting``.

When the client opens the app, the Fund Manager greets them the way a
human FM would: how you stand, what I need from you, what I'm watching.
This module PROJECTS canonical state into that shape — it authors
nothing and judges nothing:

* **book** — total from the latest portfolio snapshot; on-plan from the
  live allocation-breakdown vs the canonical ``TargetAllocationDoc``
  (ex-NVDA renormalized view: the NVDA strategic sleeve is on its own
  multi-year glide with a dedicated pace surface, so including it would
  read "off plan" for the whole transition); FI line from the canonical
  feasible-age engine (same derived-cache key as
  ``/api/retirement/projection/feasible-age`` — never recomputed with
  different assumptions).
* **needs_you** — open ``action_proposals`` whose kind/content requires
  the client (needs-info / needs-confirm semantics) + active monitor
  flags whose ``suggested_action`` asks for a client decision.
* **watching** — material flags that need nothing from the client,
  each with an explicit no-action note.
* **internal** flags (Argosy's own data/feed gaps — fx-feed blind
  spot, expense-ingestion gap) appear in NEITHER list; they stay on the
  full flags surface for the operator.

Classification here is presentation triage: small pure functions over
kind strings + payload semantics, deterministic and unit-tested. The
flags / proposals tables remain the audit truth.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.services.action_proposals import DECISION_PROPOSAL_KINDS
from argosy.state.models import ActionProposal, MonitorFlag

_log = get_logger("argosy.services.home_greeting")

# ---------------------------------------------------------------------------
# Classification — pure functions
# ---------------------------------------------------------------------------

#: Flag buckets returned by :func:`classify_flag`.
BUCKET_NEEDS_YOU = "needs_you"
BUCKET_WATCHING = "watching"
BUCKET_INTERNAL = "internal"
BUCKET_SKIP = "skip"

#: Flag kinds that are BY DEFINITION observations about Argosy's own
#: monitoring inputs (feed blind spots), not about the client's money.
_INTERNAL_FLAG_KINDS = frozenset({"state_observer_fx_observation"})

#: Payload semantics that mark an observation as a data/ingestion gap —
#: Argosy telling itself its inputs are incomplete. These stay off the
#: client greeting (they surface on the full flags surface instead).
_DATA_GAP_RE = re.compile(
    r"(data gap|ingestion gap|expense-ingestion|feed is unavailable"
    r"|no live (spot|feed)|monitoring blind|blind spot"
    r"|cannot be (monitored|validated|cross-checked))",
    re.IGNORECASE,
)

#: ``suggested_action`` values that put a flag in front of the client.
#: (Producer-emitted values like ``watchlist`` / ``monitor`` do NOT —
#: the fleet handles those itself.)
_CLIENT_ACTIONS = frozenset(
    {"needs_info", "needs_confirm", "confirm", "client_decision", "decide"}
)

#: Proposal kinds whose acceptance is a real client decision (vs. note_only /
#: set_watchlist observer chatter). Single-sourced from
#: ``action_proposals.DECISION_PROPOSAL_KINDS`` so the greeting and the inbox
#: can never disagree about which kinds need the client.
_EXECUTABLE_PROPOSAL_KINDS = DECISION_PROPOSAL_KINDS


def _rationale_text(payload: dict[str, Any]) -> str:
    return str(
        payload.get("rationale_md") or payload.get("caution") or ""
    )


def is_internal_flag(kind: str, payload: dict[str, Any]) -> bool:
    """True when the flag is about Argosy's own data gaps, not the
    client's money. Kind prefix first, payload semantics second."""
    if kind in _INTERNAL_FLAG_KINDS:
        return True
    return bool(_DATA_GAP_RE.search(_rationale_text(payload)))


def classify_flag(kind: str, severity: str, payload: dict[str, Any]) -> str:
    """Bucket one ACTIVE monitor flag for the greeting.

    Returns one of ``needs_you`` / ``watching`` / ``internal`` / ``skip``.
    Expiry filtering happens upstream (:func:`select_active_flags`) —
    this function assumes the flag is live.
    """
    if is_internal_flag(kind, payload):
        return BUCKET_INTERNAL
    if str(payload.get("suggested_action", "")).strip().lower() in _CLIENT_ACTIONS:
        return BUCKET_NEEDS_YOU
    if severity == "info":
        # Material-only: info observations stay on the full surface.
        return BUCKET_SKIP
    return BUCKET_WATCHING


def classify_proposal(
    kind: str, dedup_key: str | None, execution_state: str | None
) -> str:
    """``needs_you`` when the proposal requires the client, else ``skip``.

    * closed-loop expectation proposals (``closed_loop_*`` dedup) are
      needs-info by construction — Argosy is asking for the broker
      export;
    * ``accepted_pending_user_action`` means the client already said yes
      and the ball is in their court;
    * executable directives (allocate / rebalance / …) authored by the
      fleet need a confirm — EXCEPT the auto-derived flag-signature
      chatter (``flagsig:`` dedup keys), which is observer commentary,
      not a directive.
    """
    dk = dedup_key or ""
    if dk.startswith("closed_loop") or dk.startswith("closed-loop"):
        return BUCKET_NEEDS_YOU
    if (execution_state or "") == "accepted_pending_user_action":
        return BUCKET_NEEDS_YOU
    if kind in _EXECUTABLE_PROPOSAL_KINDS and "flagsig:" not in dk:
        return BUCKET_NEEDS_YOU
    return BUCKET_SKIP


def _fields_mention_cash(payload: dict[str, Any]) -> bool:
    fields = [str(payload.get("primary_field", ""))] + [
        str(f) for f in (payload.get("related_fields") or [])
    ]
    return any("cash" in f.lower() for f in fields)


def watching_note(
    kind: str, payload: dict[str, Any], *, has_closed_loop_needs_you: bool
) -> str:
    """The explicit "no action needed" note for a watching line.

    The deploy cash-drawdown observation is linked to the closed-loop
    "send the broker export" request when BOTH are present — the flag
    resolves with the next real ingest, and the note says so."""
    if (
        has_closed_loop_needs_you
        and kind
        in ("state_observer_position_observation", "state_observer_cash_observation")
        and _fields_mention_cash(payload)
    ):
        return "No action needed — resolves with the next broker export."
    return "No action needed — the team is monitoring."


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")

#: "~83%" style percentages inside a rationale — used to carry the concrete
#: drawdown fact into the cash headline.
_PCT_RE = re.compile(r"~?\s?(\d{1,3})\s?%")

#: Cause keywords the state observer writes for a negative-cash signature.
_OVERDEPLOY_RE = re.compile(r"over-?deploy|overdraft", re.IGNORECASE)


def _fmt_usd(v: float) -> str:
    """Clean, size-proportional money display (never cent precision)."""
    sign = "-" if v < 0 else ""
    a = abs(float(v))
    if a >= 1_000_000:
        s = f"{a / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{sign}${s}M"
    if a >= 1_000:
        s = f"{a / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{sign}${s}k"
    return f"{sign}${a:,.0f}"


def _cash_flag_headline(
    payload: dict[str, Any], negative_cash_lines: list[dict[str, Any]]
) -> str | None:
    """Fact-carrying headline for a negative-cash observer flag.

    Leads with WHICH account and the amount (resolved from the book's
    negative cash line(s)), then the concrete drawdown fact and the
    likely cause extracted from the flag rationale. Returns ``None``
    when no negative cash line exists in the book (already resolved /
    unresolvable) so the caller falls back to the generic first-sentence
    line."""
    if not negative_cash_lines:
        return None
    worst = min(
        negative_cash_lines, key=lambda l: float(l.get("usd") or 0.0)
    )
    account = str(worst.get("location") or "").strip() or "A"
    currency = str(worst.get("currency") or "").strip()
    amt = _fmt_usd(float(worst.get("usd") or 0.0))
    as_of = worst.get("snapshot_date")
    as_of_s = f" (as of {as_of})" if as_of else ""

    rationale = _rationale_text(payload)
    m = _PCT_RE.search(rationale)
    drawdown = f" while total cash drew down ~{m.group(1)}%" if m else ""
    cause = (
        "likely over-deployment/overdraft, not a routine drawdown"
        if _OVERDEPLOY_RE.search(rationale)
        else "cause not yet confirmed"
    )
    line = (
        f"{account} {currency} cash is {amt}{as_of_s} — flipped "
        f"negative{drawdown}; {cause}."
    )
    return line[:240]


def _thesis_flag_headline(payload: dict[str, Any]) -> str | None:
    """Fact-carrying headline for a thesis_monitor_* flag: ticker, status,
    and the top concrete signals from the payload."""
    ticker = str(payload.get("ticker", "")).strip()
    if not ticker:
        return None
    status = str(payload.get("thesis_status", "")).strip() or "flagged"
    signals = [
        str(s).strip() for s in (payload.get("signals") or []) if str(s).strip()
    ]
    if not signals:
        return None
    line = f"{ticker} thesis {status} — {'; '.join(signals[:2])}."
    return line[:240]


def headline_for_flag(
    kind: str,
    payload: dict[str, Any],
    *,
    negative_cash_lines: list[dict[str, Any]] | None = None,
) -> str:
    """Greeting headline for a flag — leads with the concrete facts.

    Per-kind formatters pull WHICH account / amount / signals / likely
    cause out of the flag payload (plus the book for cash lines); any
    kind without a formatter — or a formatter that can't resolve its
    facts — falls back to the flag summary's first sentence
    (:func:`_one_line`)."""
    try:
        if kind.startswith("thesis_monitor_"):
            line = _thesis_flag_headline(payload)
            if line:
                return line
        elif kind in (
            "state_observer_position_observation",
            "state_observer_cash_observation",
        ) and _fields_mention_cash(payload):
            line = _cash_flag_headline(payload, negative_cash_lines or [])
            if line:
                return line
    except Exception:  # noqa: BLE001 — a formatter bug must never sink the greeting
        _log.warning("home_greeting.headline_formatter_failed", exc_info=True)
    return _one_line(payload, kind)


def _negative_cash_lines(session: Session, user_id: str) -> list[dict[str, Any]]:
    """Negative cash balances from the latest snapshot's positions —
    ``[{location, currency, usd, snapshot_date}]``. Empty on any gap."""
    try:
        from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row

        row = get_latest_snapshot_row(session, user_id)
        if row is None or not row.positions_json:
            return []
        positions = json.loads(row.positions_json)
        if not isinstance(positions, list):
            return []
        out: list[dict[str, Any]] = []
        for p in positions:
            if not isinstance(p, dict):
                continue
            if str(p.get("asset_type", "")).strip().lower() != "cash":
                continue
            v_k = p.get("usd_value_k")
            if not isinstance(v_k, (int, float)) or v_k >= 0:
                continue
            out.append(
                {
                    "location": p.get("location"),
                    "currency": p.get("currency"),
                    "usd": float(v_k) * 1000.0,
                    "snapshot_date": getattr(row, "snapshot_date", None),
                }
            )
        return out
    except Exception:  # noqa: BLE001 — enrichment only
        _log.warning("home_greeting.negative_cash_lookup_failed", exc_info=True)
        return []


def _one_line(payload: dict[str, Any], kind: str) -> str:
    """A single greeting-grade line for a flag: ticker-prefixed first
    sentence of the rationale, markdown-stripped, hard-capped."""
    text = _rationale_text(payload).replace("**", "").replace("\n", " ").strip()
    first = _SENTENCE_END_RE.split(text, maxsplit=1)[0].strip() if text else ""
    ticker = str(payload.get("ticker", "")).strip()
    if ticker and not first.upper().startswith(ticker.upper()):
        status = str(payload.get("thesis_status", "")).strip()
        prefix = f"{ticker} thesis {status}: " if status else f"{ticker}: "
        first = prefix + first
    if not first:
        first = kind.replace("_", " ")
    if len(first) > 200:
        first = first[:200].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return first


# ---------------------------------------------------------------------------
# Selection — mirrors the canonical active-flags filter
# ---------------------------------------------------------------------------


def select_active_flags(
    session: Session, user_id: str, *, now: datetime | None = None
) -> list[MonitorFlag]:
    """Active + unacknowledged + unexpired flags, the same liveness rule
    as ``GET /api/retirement/monitor/flags`` (an expired-by-backfill row
    like the 38-day-old alpha caution never reaches classification)."""
    now_dt = now or datetime.now(UTC)
    rows = (
        session.execute(
            select(MonitorFlag)
            .where(MonitorFlag.user_id == user_id)
            .where(MonitorFlag.status == "active")
            .where(MonitorFlag.acknowledged_at.is_(None))
            .order_by(MonitorFlag.surfaced_at.desc())
        )
        .scalars()
        .all()
    )
    out: list[MonitorFlag] = []
    for r in rows:
        if r.expires_at is not None:
            expires = r.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= now_dt:
                continue
        payload = _flag_payload(r)
        if r.kind == "mc_regression" and payload.get("fired") is not True:
            continue
        out.append(r)
    return out


def _flag_payload(row: MonitorFlag) -> dict[str, Any]:
    try:
        parsed = json.loads(row.payload)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Book line — canonical sources only
# ---------------------------------------------------------------------------

#: A class more than this many percentage points from its target (over
#: the ex-NVDA renormalized book) is out of band for the greeting line.
ON_PLAN_BAND_PP = 5.0


def _book_total_usd(session: Session, user_id: str) -> tuple[float | None, str | None]:
    """(total USD, ISO snapshot date) from the latest snapshot row."""
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row

    row = get_latest_snapshot_row(session, user_id)
    if row is None:
        return None, None
    as_of = getattr(row, "snapshot_date", None)
    as_of_s = as_of.isoformat() if as_of is not None else None
    try:
        totals = json.loads(row.totals_json or "{}")
        return float(totals.get("total_usd_value_k", 0.0)) * 1000.0, as_of_s
    except (TypeError, ValueError):
        return None, as_of_s


def _on_plan(session: Session, user_id: str) -> tuple[bool, str]:
    """(on_plan, note) from the live breakdown vs the canonical doc.

    Ex-NVDA renormalized comparison — the NVDA strategic sleeve is a
    governed multi-year glide with its own pace surface; judged against
    its END-state target the book would read "off plan" for the whole
    transition, which is noise, not signal."""
    from argosy.services.allocation_breakdown import build_allocation_breakdown
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        row_to_snapshot,
    )
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    row = get_latest_snapshot_row(session, user_id)
    if row is None:
        return False, "no portfolio snapshot yet"
    pv = get_current_plan(session, user_id)
    if pv is None:
        return False, "no current plan"
    doc = load_plan_target_allocation(pv)
    rows = build_allocation_breakdown(
        row_to_snapshot(row), doc, exclude_nvda=True
    )
    if not rows:
        return False, "no allocation data"

    worst = max(
        (r for r in rows if r.target_pct is not None),
        key=lambda r: abs(r.current_pct - (r.target_pct or 0.0)),
        default=None,
    )
    if worst is None:
        return False, "no plan targets"
    gap = abs(worst.current_pct - (worst.target_pct or 0.0))
    if gap <= ON_PLAN_BAND_PP:
        return True, f"all classes within ±{ON_PLAN_BAND_PP:g}pp of target (ex-NVDA glide)"
    return False, (
        f"transition in progress — biggest gap: {worst.label} "
        f"{worst.current_pct:.1f}% vs {worst.target_pct:.1f}% target"
    )


def _fi_line(session: Session, user_id: str, *, now: datetime) -> str:
    """The canonical FI headline — same engine + derived-cache key as
    ``GET /api/retirement/projection/feasible-age``; never recomputed
    with different assumptions."""
    try:
        from argosy.services import derived_cache
        from argosy.services.retirement.retirement_plan import (
            RetirementAssumptions,
            canonical_feasible_dual_track,
        )

        def _compute() -> dict:
            r = canonical_feasible_dual_track(
                session=session,
                user_id=user_id,
                target_p_solvent=0.90,
                assumptions=RetirementAssumptions(n_paths=1500, seed=42),
            )
            return {
                "earliest_feasible_age": r.earliest_feasible_age,
                "current_age": r.current_age,
            }

        version = derived_cache.version_tuple(session, user_id)
        if version is not None:
            version = version + ("feasible-age", 0.90, 1500, 42)
        # NOTE: same tag+key as /api/retirement/projection/feasible-age —
        # a warm dashboard cache serves the greeting for free. The dict
        # cached by that route is a superset of the two keys read here.
        val = derived_cache.get_or_compute(
            "retirement.feasible-age", version, _compute
        )
        earliest = val.get("earliest_feasible_age")
        current = val.get("current_age")
        if earliest is None or current is None:
            return "FI track: —"
        years_out = max(0.0, float(earliest) - float(current))
        fi_year = (now + timedelta(days=365.25 * years_out)).year
        return f"FI track: {fi_year} (age {float(earliest):g})"
    except Exception:
        _log.warning("home_greeting.fi_line_unavailable", exc_info=True)
        return "FI track: —"


# ---------------------------------------------------------------------------
# Next scheduled review
# ---------------------------------------------------------------------------

#: Cadence fields (``CadencesBlock`` attribute names) the client would
#: recognize as "Argosy reviewing my situation".
_REVIEW_CADENCE_FIELDS = (
    "daily_brief",
    "watchlist",
    "plan_watcher",
    "news_daily",
    "state_observer",
    "weekly_review",
    "monthly_cycle",
)


def _next_review_local(user_id: str, *, now: datetime) -> str | None:
    """Earliest next cron fire across the review cadences, formatted in
    the cadence's local timezone ("17:00" today, "Tue 17:00" later)."""
    try:
        from argosy.agent_settings import load_agent_settings
        from argosy.orchestrator.loops.base import LoopSchedule

        settings = load_agent_settings(user_id)
        cadences = settings.cadences
        best: datetime | None = None
        best_tz = "Asia/Jerusalem"
        for field in _REVIEW_CADENCE_FIELDS:
            cfg = getattr(cadences, field, None)
            if cfg is None or not cfg.enabled or not cfg.cron:
                continue
            due = LoopSchedule.from_config(cfg).next_due_after(now)
            if best is None or due < best:
                best = due
                best_tz = cfg.timezone
        if best is None:
            return None
        local = best.astimezone(ZoneInfo(best_tz))
        now_local = now.astimezone(ZoneInfo(best_tz))
        if local.date() == now_local.date():
            return local.strftime("%H:%M")
        return local.strftime("%a %H:%M")
    except Exception:
        _log.warning("home_greeting.next_review_unavailable", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# needs_you / watching item builders
# ---------------------------------------------------------------------------


def _proposal_cta(kind: str, dedup_key: str | None) -> dict[str, str]:
    dk = dedup_key or ""
    if dk.startswith("closed_loop") or dk.startswith("closed-loop"):
        return {"label": "Send the broker export", "href": "/inbox"}
    if kind == "allocate":
        return {"label": "Open the deploy tool", "href": "/inbox#deploy-cash"}
    if kind == "rebalance":
        return {"label": "Review the trade plan", "href": "/inbox"}
    return {"label": "Review", "href": "/inbox"}


def _needs_you_from_proposal(p: ActionProposal) -> dict[str, Any]:
    return {
        "id": f"proposal:{p.id}",
        "kind": p.kind,
        "headline": (p.summary or "").strip()[:240],
        "why_md": p.rationale_md or "",
        "cta": _proposal_cta(p.kind, p.dedup_key),
        "tone": "decision",
    }


def _needs_you_from_verified_items(
    session: Session, user_id: str, *, now: datetime
) -> list[dict[str, Any]]:
    """Needs-confirm entries for plan action items Argosy verified as
    looks-executed from the book/fills evidence.

    NO auto-ack — the client confirms; each entry carries the payload
    for the existing ``POST /api/plan/action-items/{item_id}/ack``
    endpoint. A confirmed item stops appearing (its ack row matches)."""
    try:
        from argosy.services.action_item_evidence import (
            looks_executed_unconfirmed_items,
        )

        items = looks_executed_unconfirmed_items(
            session, user_id, today=now.date()
        )
    except Exception:  # noqa: BLE001 — enrichment only
        _log.warning("home_greeting.verified_items_failed", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        out.append(
            {
                "id": f"action_item:{it.item_id}",
                "kind": "action_item_confirm",
                "headline": f"Looks executed — confirm: {it.label}"[:240],
                "why_md": it.argosy_verified_summary or "",
                "cta": {"label": "Confirm done", "href": "/#action-items"},
                "tone": "confirm",
                # Everything the UI needs to hit the existing ack endpoint.
                "ack": {
                    "method": "POST",
                    "endpoint": f"/api/plan/action-items/{it.item_id}/ack",
                    "content_fingerprint": it.content_fingerprint,
                    "user_id": user_id,
                },
            }
        )
    return out


def _fmt_since(d: Any) -> str:
    """"Jun 17" — short, day-precision overdue anchor."""
    try:
        return f"{d.strftime('%b')} {d.day}"
    except Exception:  # noqa: BLE001 — display only
        return str(d)


def _needs_you_from_overdue_items(
    session: Session, user_id: str, *, now: datetime
) -> list[dict[str, Any]]:
    """Needs-ACTION entries for plan action items past their due date
    with NO execution evidence in the book/fills.

    The other half of the closed loop: looks-executed items get a
    needs-confirm above; a genuinely unexecuted overdue item (e.g. the
    June-17 vest sale with no NVDA sale on the Schwab ledger) must
    reach the client too — "you need to do this, it's overdue".

    One decision = one row: each entry is keyed by the item's stable
    ``item_id``, so the SAME item stays ONE row across days (the
    headline carries "overdue since <due date>" — no per-day spam).
    """
    try:
        from argosy.services.action_item_evidence import overdue_unexecuted_items

        items = overdue_unexecuted_items(session, user_id, today=now.date())
    except Exception:  # noqa: BLE001 — enrichment only
        _log.warning("home_greeting.overdue_items_failed", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        label = (it.label or "").strip()
        # "You need to sell the June vest — overdue since Jun 17".
        verb_phrase = (label[0].lower() + label[1:]) if label else "act on this item"
        since = f" — overdue since {_fmt_since(it.dated)}" if it.dated else " — overdue"
        headline = f"You need to {verb_phrase}"[: 240 - len(since)] + since
        why_parts: list[str] = []
        if (it.detail or "").strip():
            why_parts.append(it.detail.strip())
        due_s = it.dated.isoformat() if it.dated else "its due date"
        why_parts.append(
            f"This was due {due_s} and I found no execution evidence in "
            "the book or fills — it still needs you."
        )
        if (it.how_to or "").strip():
            why_parts.append(f"**How:** {it.how_to.strip()}")
        if (it.done_when or "").strip():
            why_parts.append(f"**Done when:** {it.done_when.strip()}")
        out.append(
            {
                "id": f"action_item:{it.item_id}",
                "kind": "action_item_overdue",
                "headline": headline,
                "why_md": "\n\n".join(why_parts),
                "cta": {"label": "Open the checklist", "href": "/#action-items"},
                "tone": "decision",
            }
        )
    return out


def _needs_you_from_flag(
    f: MonitorFlag,
    payload: dict[str, Any],
    *,
    negative_cash_lines: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"flag:{f.id}",
        "kind": f.kind,
        "headline": headline_for_flag(
            f.kind, payload, negative_cash_lines=negative_cash_lines
        ),
        "why_md": _rationale_text(payload),
        "cta": {"label": "Decide", "href": "/inbox"},
        "tone": "decision",
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_greeting(
    session: Session, user_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Assemble the FM first-greeting payload. Read-only projection."""
    from argosy.services.action_proposals import list_open_action_proposals

    now_dt = now or datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)

    # --- needs_you: proposals first (the FM's own asks), then flags.
    needs_you: list[dict[str, Any]] = []
    proposals = list_open_action_proposals(session, user_id)
    for p in proposals:
        if classify_proposal(p.kind, p.dedup_key, p.execution_state) == BUCKET_NEEDS_YOU:
            needs_you.append(_needs_you_from_proposal(p))
    has_closed_loop = any(
        str(i["id"]).startswith("proposal:")
        and (i["cta"]["label"] == "Send the broker export")
        for i in needs_you
    )

    # Plan action items that look ALREADY EXECUTED (book/fills evidence):
    # a one-click needs-confirm, never an auto-ack.
    needs_you.extend(
        _needs_you_from_verified_items(session, user_id, now=now_dt)
    )

    # Plan action items past due with NO execution evidence: genuinely
    # OVERDUE — the client must act ("you need to sell the June vest —
    # overdue since Jun 17"). Mutually exclusive with the looks-executed
    # entries above (positive evidence demotes an item from overdue).
    needs_you.extend(
        _needs_you_from_overdue_items(session, user_id, now=now_dt)
    )

    # --- flags: classify each active flag once. Negative cash lines are
    # resolved once from the book so per-kind headlines can name the
    # account + amount instead of a vague "a USD cash account flipped".
    neg_cash = _negative_cash_lines(session, user_id)
    watching: list[dict[str, Any]] = []
    for f in select_active_flags(session, user_id, now=now_dt):
        payload = _flag_payload(f)
        bucket = classify_flag(f.kind, f.severity, payload)
        if bucket == BUCKET_NEEDS_YOU:
            needs_you.append(
                _needs_you_from_flag(f, payload, negative_cash_lines=neg_cash)
            )
        elif bucket == BUCKET_WATCHING:
            watching.append(
                {
                    "id": f"flag:{f.id}",
                    "headline": headline_for_flag(
                        f.kind, payload, negative_cash_lines=neg_cash
                    ),
                    "note": watching_note(
                        f.kind, payload, has_closed_loop_needs_you=has_closed_loop
                    ),
                }
            )
        # internal / skip: stay on the full flags surface only.

    on_plan, on_plan_note = _on_plan(session, user_id)
    total_usd, book_as_of = _book_total_usd(session, user_id)

    return {
        "greeting_name": user_id.strip().capitalize() or user_id,
        "book": {
            "total_usd": total_usd,
            "on_plan": on_plan,
            "on_plan_note": on_plan_note,
            "fi_line": _fi_line(session, user_id, now=now_dt),
            "as_of": book_as_of,
        },
        "needs_you": needs_you,
        "watching": watching,
        "quiet": not needs_you and not watching,
        "next_review_local": _next_review_local(user_id, now=now_dt),
    }


__all__ = [
    "BUCKET_INTERNAL",
    "BUCKET_NEEDS_YOU",
    "BUCKET_SKIP",
    "BUCKET_WATCHING",
    "ON_PLAN_BAND_PP",
    "build_greeting",
    "classify_flag",
    "headline_for_flag",
    "classify_proposal",
    "is_internal_flag",
    "select_active_flags",
    "watching_note",
]
