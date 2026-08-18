"""READ-time ``{{fact:key}}`` rendering — plan text stays tokenised; numbers
come from the live book.

Owner directive (2026-07-12): a trade must not rewrite the plan. Persisted
plan surfaces carry ``{{fact:key}}`` tokens; this module resolves them from
``resolve_plan_numbers`` (live snapshot) at API / DTO assembly time, with
per-fact provenance and a staleness seam when a rendered fact crosses a
recorded FI claim boundary (``fi_shock_qualifier`` detection).

Cache key: ``(plan_version_id, snapshot_id)``. Unresolvable keys render as
``[derivation pending]`` with a loud log — never a silent wrong number.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

from argosy.quality.fact_registry import (
    FACT_DISPLAY,
    FACT_SOURCE_ALIAS,
    PENDING_LABEL,
    PlaceholderError,
    format_fact,
    strip_emission_scaffolding,
)
from argosy.quality.fi_shock_qualifier import (
    _FX_SHOCK_QUALIFIER_RE,
    _NEGATION_RE,
    _REACHED_RE,
    _SENTENCE_KEEP_RE,
    _SHOCK_QUALIFIER_RE,
    shock_needs_qualifiers,
)

log = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[int, int | None], "FactRenderBundle"] = {}
_CACHE_MAX = 64

PLAN_LOGIC_STALE_KIND = "plan_logic_stale"
PLAN_LOGIC_STALE_DEDUP = "v1|plan_logic_stale|{user_id}|{plan_id}"


@dataclass(frozen=True)
class FactProvenance:
    key: str
    display: str
    source_locator: str
    status: str
    value: float | None = None


@dataclass
class StalenessFinding:
    """A rendered fact crossed a prose claim boundary — corrective needed."""

    claim: str
    detail: str
    fact_key: str | None = None


@dataclass
class FactRenderBundle:
    """Rendered plan surfaces + provenance for one (plan, snapshot) pair."""

    plan_version_id: int
    snapshot_id: int | None
    horizon_long_md: str | None
    horizon_medium_md: str | None
    horizon_short_md: str | None
    sections_json: str | None
    narrative_md: str | None
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_keys: list[str] = field(default_factory=list)
    staleness: list[StalenessFinding] = field(default_factory=list)


def _latest_snapshot_id(session, user_id: str) -> int | None:
    try:
        return session.execute(
            sa.text(
                "select id from portfolio_snapshots where user_id=:u "
                "order by snapshot_date desc, id desc limit 1"
            ),
            {"u": user_id},
        ).scalar()
    except Exception:  # noqa: BLE001 — cache key degrades to None
        return None


def clear_fact_render_cache() -> None:
    """Test helper — drop the process-local render cache."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_get(key: tuple[int, int | None]) -> FactRenderBundle | None:
    with _CACHE_LOCK:
        return _CACHE.get(key)


def _cache_put(key: tuple[int, int | None], bundle: FactRenderBundle) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX and key not in _CACHE:
            # Drop an arbitrary oldest entry (insertion order on 3.7+).
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[key] = bundle


def _render_text_with_provenance(
    text: str | None,
    resolved,
    *,
    pending_keys: list[str],
    provenance: dict[str, dict[str, Any]],
) -> str | None:
    """Substitute tokens; unresolvable → PENDING_LABEL + loud log."""
    if text is None:
        return None
    if "{{fact:" not in text:
        return text

    def _sub(m):
        key = m.group(1)
        display = FACT_DISPLAY.get(key)
        source_key = FACT_SOURCE_ALIAS.get(key, key)
        try:
            if display is None:
                raise PlaceholderError(f"fact key not in registry: {key!r}")
            rv = resolved.get(source_key)
            if (
                rv is None
                or getattr(rv, "status", None) != "resolved"
                or getattr(rv, "value", None) is None
            ):
                raise PlaceholderError(
                    f"fact {key!r} is not resolved "
                    f"(status={getattr(rv, 'status', 'MISSING')!r})"
                )
            rendered = format_fact(
                rv.value, getattr(rv, "unit", ""), display=display
            )
            provenance[key] = {
                "display": rendered,
                "value": float(rv.value),
                "unit": getattr(rv, "unit", None),
                "source_locator": getattr(rv, "source_locator", "") or "",
                "status": "resolved",
                "resolver_key": source_key,
            }
            return rendered
        except PlaceholderError as exc:
            pending_keys.append(key)
            provenance[key] = {
                "display": PENDING_LABEL,
                "value": None,
                "unit": None,
                "source_locator": "",
                "status": "pending",
                "resolver_key": source_key,
                "error": str(exc)[:200],
            }
            log.warning(
                "fact_token_render.unresolvable key=%s error=%s",
                key,
                str(exc)[:200],
            )
            return PENDING_LABEL

    import re

    sanitized = strip_emission_scaffolding(text)
    # Local sub (not fact_registry.render_placeholders) so we keep provenance
    # and never leave a raw token or silent wrong number.
    placeholder_re = re.compile(r"\{\{fact:([A-Za-z0-9_.]+)\}\}")
    rendered = placeholder_re.sub(_sub, sanitized)
    age_doubling = re.compile(r"\bage[\s-]+age(\s+\d)")
    return age_doubling.sub(r"age\1", rendered)


def _render_sections_json(
    sections_json: str | None,
    resolved,
    *,
    pending_keys: list[str],
    provenance: dict[str, dict[str, Any]],
) -> str | None:
    if not sections_json:
        return sections_json
    try:
        sections = json.loads(sections_json)
    except (TypeError, ValueError):
        return sections_json
    if not isinstance(sections, list):
        return sections_json
    changed = False
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        body = sec.get("body_md")
        if isinstance(body, str) and "{{fact:" in body:
            sec["body_md"] = _render_text_with_provenance(
                body, resolved, pending_keys=pending_keys, provenance=provenance,
            )
            changed = True
        title = sec.get("title")
        if isinstance(title, str) and "{{fact:" in title:
            sec["title"] = _render_text_with_provenance(
                title, resolved, pending_keys=pending_keys, provenance=provenance,
            )
            changed = True
    if not changed:
        return sections_json
    return json.dumps(sections, ensure_ascii=False)


def detect_claim_boundary_crossings(
    text: str,
    resolved,
    *,
    shock_result: dict | None = None,
    fx_shock_result: dict | None = None,
) -> list[StalenessFinding]:
    """If prose asserts FI 'reached' but the live book disagrees (or shock
    needs a qualifier the sentence lacks), the plan logic is stale.

    Sentence-scoped — same doctrine as ``fi_shock_qualifier`` /
    ``coherence_gate.check_fi_sufficiency_under_shock``.
    """
    if not text:
        return []
    findings: list[StalenessFinding] = []
    need_nvda, need_fx = shock_needs_qualifiers(
        shock_result=shock_result, fx_shock_result=fx_shock_result,
    )
    margin = resolved.get("retirement.fi_margin_signed_nis")
    margin_neg = (
        margin is not None
        and getattr(margin, "status", None) == "resolved"
        and margin.value is not None
        and float(margin.value) < 0
    )

    parts = _SENTENCE_KEEP_RE.split(text)
    i = 0
    while i < len(parts):
        sentence = parts[i]
        if i + 1 < len(parts):
            sentence = sentence + parts[i + 1]
            i += 2
        else:
            i += 1
        m = _REACHED_RE.search(sentence)
        if not m:
            continue
        window = sentence[max(0, m.start() - 40): m.end()]
        if _NEGATION_RE.search(window):
            continue
        claim = m.group(0)
        if margin_neg:
            findings.append(StalenessFinding(
                claim=claim,
                detail=(
                    "prose asserts FI reached but live "
                    "retirement.fi_margin_signed_nis is negative — "
                    "plan logic stale; corrective needed"
                ),
                fact_key="retirement.fi_margin_signed_nis",
            ))
            continue
        has_nvda_q = bool(_SHOCK_QUALIFIER_RE.search(sentence))
        has_fx_q = bool(_FX_SHOCK_QUALIFIER_RE.search(sentence))
        if need_nvda and not has_nvda_q:
            findings.append(StalenessFinding(
                claim=claim,
                detail=(
                    "prose asserts FI reached without NVDA-shock qualifier "
                    "but live shock_0.30.perpetuity_reached is False — "
                    "plan logic stale; corrective needed"
                ),
                fact_key="retirement.fi_shock_net_worth_nis",
            ))
        elif need_fx and not has_fx_q:
            findings.append(StalenessFinding(
                claim=claim,
                detail=(
                    "prose asserts FI reached without FX-shock qualifier "
                    "but live fx shock total_reached is False — "
                    "plan logic stale; corrective needed"
                ),
                fact_key="retirement.fi_fx_shock_net_worth_nis",
            ))
    return findings


def _shock_rows_from_resolved(resolved) -> tuple[dict | None, dict | None]:
    """Best-effort rebuild of shock result dicts from resolver keys."""

    def _v(key: str):
        rv = resolved.get(key)
        if rv is None or getattr(rv, "status", None) != "resolved":
            return None
        return getattr(rv, "value", None)

    shock = None
    fx = None
    try:
        shock_nw = _v("retirement.fi_shock_net_worth_nis")
        fi_total = _v("retirement.fi_total_capital_nis")
        if shock_nw is not None and fi_total is not None:
            perpetuity = _v("retirement.fi_perpetuity_nis") or fi_total
            shock = {
                "shock_0.30": {
                    "perpetuity_reached": float(shock_nw) >= float(perpetuity),
                }
            }
        fx_nw = _v("retirement.fi_fx_shock_net_worth_nis")
        if fx_nw is not None and fi_total is not None:
            fx = {
                "fx_shock_-0.10": {
                    "total_reached": float(fx_nw) >= float(fi_total),
                }
            }
    except Exception:  # noqa: BLE001
        return None, None
    return shock, fx


def write_plan_logic_stale_flag(
    session,
    *,
    user_id: str,
    plan_version_id: int,
    findings: list[StalenessFinding],
) -> Any | None:
    """Upsert an active ``plan_logic_stale`` monitor flag (deduped)."""
    if not findings:
        return None
    from argosy.state.models import MonitorFlag

    dedup = PLAN_LOGIC_STALE_DEDUP.format(
        user_id=user_id, plan_id=plan_version_id,
    )
    now = datetime.now(timezone.utc)
    existing = session.execute(
        sa.select(MonitorFlag)
        .where(
            MonitorFlag.user_id == user_id,
            MonitorFlag.dedup_key == dedup,
            MonitorFlag.status == "active",
        )
        .order_by(MonitorFlag.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    payload = {
        "plan_version_id": plan_version_id,
        "message": "plan logic stale — corrective needed",
        "findings": [
            {"claim": f.claim, "detail": f.detail, "fact_key": f.fact_key}
            for f in findings
        ],
    }
    if existing is not None:
        existing.payload = json.dumps(payload)
        existing.surfaced_at = now
        existing.severity = "warning"
        return existing
    row = MonitorFlag(
        user_id=user_id,
        kind=PLAN_LOGIC_STALE_KIND,
        severity="warning",
        payload=json.dumps(payload),
        surfaced_at=now,
        expires_at=now + timedelta(days=30),
        dedup_key=dedup,
        status="active",
    )
    session.add(row)
    return row


def render_plan_facts(
    session,
    *,
    user_id: str,
    plan_version,
    write_staleness_flag: bool = True,
    use_cache: bool = True,
) -> FactRenderBundle:
    """Resolve ``{{fact:key}}`` tokens on a plan version from the LIVE book.

    Numbers come from ``resolve_plan_numbers`` against the current snapshot
    (not the synthesis-time decision run), so a trade updates rendered
    figures without mutating the plan text. Cache key =
    ``(plan_version.id, latest_snapshot_id)``.

    This plan version's own ``decision_run_id`` (may be ``None``) IS passed
    through to ``resolve_plan_numbers`` so it can read the canonical
    ``target_allocation_json`` doc this plan's own draft was rendered from
    (the settled binding NVDA cap + per-class allocation targets) — the only
    keys that additionally resolves. All snapshot/live-book resolution stays
    unconditional and untouched by this. If ``decision_run_id`` is ``None``
    (older/imported plans), behaviour is unchanged from before.
    """
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers

    plan_id = int(plan_version.id)
    snapshot_id = _latest_snapshot_id(session, user_id)
    cache_key = (plan_id, snapshot_id)
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            return hit

    # Live book for holdings/FX/etc (a trade must not require a
    # re-synthesis to update numbers) — those resolvers run unconditionally
    # against the current snapshot regardless of decision_run_id. We DO pass
    # this plan version's own decision_run_id (None for older/imported plans,
    # unchanged behaviour) so `_apply_canonical_allocation` can read the
    # target_allocation_json this plan's own draft was rendered from — the
    # ONLY thing that publishes the settled binding NVDA cap and per-class
    # allocation targets; without it those keys stay pending forever
    # (`[derivation pending]`) for every plan that has one. Confirmed by
    # reading resolve_plan_numbers: role-sourced AgentReport keys (savings,
    # spend, withdrawal, etc.) are looked up under decision_id=f"plan-synth-
    # {decision_run_id}" plus a phase-reuse donor chain; for amendment runs
    # (e.g. 400-403) there are zero agent_reports rows and no donor lineage
    # stamped, so those keys stay pending exactly as before — only the
    # canonical-allocation-doc keys newly resolve.
    decision_run_id = getattr(plan_version, "decision_run_id", None)
    try:
        resolved = resolve_plan_numbers(
            session, user_id=user_id, decision_run_id=decision_run_id,
            include_canonical_ages=True,
        )
    except Exception as exc:  # noqa: BLE001 — fail to pending, never wrong digits
        log.warning(
            "fact_token_render.resolve_failed user=%s plan=%s error=%s",
            user_id, plan_id, str(exc)[:200],
        )
        bundle = FactRenderBundle(
            plan_version_id=plan_id,
            snapshot_id=snapshot_id,
            horizon_long_md=getattr(plan_version, "horizon_long_md", None),
            horizon_medium_md=getattr(plan_version, "horizon_medium_md", None),
            horizon_short_md=getattr(plan_version, "horizon_short_md", None),
            sections_json=getattr(plan_version, "sections_json", None),
            narrative_md=_narrative_md(plan_version),
        )
        if use_cache:
            _cache_put(cache_key, bundle)
        return bundle

    pending: list[str] = []
    provenance: dict[str, dict[str, Any]] = {}
    long_md = _render_text_with_provenance(
        getattr(plan_version, "horizon_long_md", None),
        resolved, pending_keys=pending, provenance=provenance,
    )
    med_md = _render_text_with_provenance(
        getattr(plan_version, "horizon_medium_md", None),
        resolved, pending_keys=pending, provenance=provenance,
    )
    short_md = _render_text_with_provenance(
        getattr(plan_version, "horizon_short_md", None),
        resolved, pending_keys=pending, provenance=provenance,
    )
    sections = _render_sections_json(
        getattr(plan_version, "sections_json", None),
        resolved, pending_keys=pending, provenance=provenance,
    )
    narrative = _render_text_with_provenance(
        _narrative_md(plan_version),
        resolved, pending_keys=pending, provenance=provenance,
    )

    combined = "\n".join(
        t for t in (long_md, med_md, short_md, narrative or "") if t
    )
    if sections and "{{fact:" not in (getattr(plan_version, "sections_json", None) or ""):
        # bodies already rendered; still scan rendered text for claims
        try:
            for sec in json.loads(sections or "[]"):
                if isinstance(sec, dict) and sec.get("body_md"):
                    combined += "\n" + str(sec["body_md"])
        except (TypeError, ValueError):
            pass
    else:
        try:
            raw_sections = json.loads(
                getattr(plan_version, "sections_json", None) or "[]"
            )
            for sec in raw_sections:
                if isinstance(sec, dict) and sec.get("body_md"):
                    combined += "\n" + str(sec["body_md"])
        except (TypeError, ValueError):
            pass

    shock, fx = _shock_rows_from_resolved(resolved)
    # Prefer raw (token) text for claim detection so we judge the AUTHOR'S
    # claim, not the rendered digits; fall back to rendered if no tokens.
    claim_text = "\n".join(
        t for t in (
            getattr(plan_version, "horizon_long_md", None) or "",
            getattr(plan_version, "horizon_medium_md", None) or "",
            getattr(plan_version, "horizon_short_md", None) or "",
            _narrative_md(plan_version) or "",
        ) if t
    )
    try:
        for sec in json.loads(getattr(plan_version, "sections_json", None) or "[]"):
            if isinstance(sec, dict) and sec.get("body_md"):
                claim_text += "\n" + str(sec["body_md"])
    except (TypeError, ValueError):
        pass
    # After render, claims that used tokens still assert "reached" in prose
    # around them — scan both raw and rendered.
    staleness = detect_claim_boundary_crossings(
        claim_text + "\n" + combined,
        resolved,
        shock_result=shock,
        fx_shock_result=fx,
    )
    # Dedup by detail
    seen: set[str] = set()
    uniq: list[StalenessFinding] = []
    for f in staleness:
        if f.detail in seen:
            continue
        seen.add(f.detail)
        uniq.append(f)
    staleness = uniq

    if write_staleness_flag and staleness:
        try:
            write_plan_logic_stale_flag(
                session,
                user_id=user_id,
                plan_version_id=plan_id,
                findings=staleness,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 — flag is best-effort
            log.warning(
                "fact_token_render.staleness_flag_failed user=%s plan=%s "
                "error=%s",
                user_id, plan_id, str(exc)[:200],
            )
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass

    bundle = FactRenderBundle(
        plan_version_id=plan_id,
        snapshot_id=snapshot_id,
        horizon_long_md=long_md,
        horizon_medium_md=med_md,
        horizon_short_md=short_md,
        sections_json=sections,
        narrative_md=narrative,
        provenance=provenance,
        pending_keys=sorted(set(pending)),
        staleness=staleness,
    )
    if use_cache:
        _cache_put(cache_key, bundle)
    return bundle


def _narrative_md(plan_version) -> str | None:
    """Pull English narrative body if persisted (migration 0062)."""
    raw = getattr(plan_version, "narrative_json", None)
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("narrative_md_en", "en", "md_en", "body_en", "narrative_md"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


__all__ = [
    "FactProvenance",
    "FactRenderBundle",
    "PLAN_LOGIC_STALE_KIND",
    "StalenessFinding",
    "clear_fact_render_cache",
    "detect_claim_boundary_crossings",
    "render_plan_facts",
    "write_plan_logic_stale_flag",
]
