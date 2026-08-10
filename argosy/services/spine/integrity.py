"""Spine PRODUCER 1 (partial) — the snapshot integrity floor.

Operating-model spec §2A "SPINE PRODUCERS" + §3 "Integrity floor". Provides:

  * :func:`compute_snapshot_content_hash` — a stable SHA-256 over the WHOLE
    normalized snapshot (every money/partition field of every position, the
    snapshot-level ``snapshot_date``, and the totals payload). Reorder-invariant;
    any changed money/partition fact changes the hash. Numbers use a lossless
    canonical repr (distinct share counts never collide).
  * :func:`assess_snapshot_integrity` — pass/fail, REUSING
    ``argosy.services.holding_books`` for conservation
    (``assess_snapshot_ingest`` for the catastrophic-drop + account-coverage
    scoping, ``books_consistency_check_positions`` for dup/ambiguous-blank, and
    the ``load_total_book`` degrade signal), plus explicit ``>=20%`` total-value
    (incl. cash) / count / account-coverage / currency-coverage drop checks, an
    unparseable-book check, a corrupt-typed-field check, and a per-item
    ``shares×price≈value_local`` sanity check.
  * :func:`record_integrity_verdict` — hash + assess, append an immutable verdict
    (``verdict_seq`` = prior max + 1) and CAS-advance ``integrity_verdict_head``
    with an expected-old-seq predicate; a lost CAS ROLLS BACK the whole
    transaction (no orphan verdict) and raises.

Independent-source checks the spec names but has no feed for (ISIN/CUSIP stable
IDs, broker-signed source records, broker account totals, event-set manifest)
are recorded as unavailable TODOs, NEVER as failures.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

from argosy.logging import get_logger
from argosy.services.holding_books import (
    SnapshotIngestRejected,
    _as_date,
    _as_mapping,
    _location_of,
    _symbol_of,
    accounts_covered_from_positions,
    assess_snapshot_ingest,
    books_consistency_check_positions,
    investable_usd_k,
    load_total_book,
    location_account_key,
    position_usd_value_k,
)

log = get_logger(__name__)

# Spec §3: reject "at or beyond a 20% drop … vs the prior live row" — the
# comparison is ``>=``. Recorded on every verdict for provenance (§3, defect 6).
_SPINE_DROP_FRACTION = 0.20
_RETAIN_FRACTION = 1.0 - _SPINE_DROP_FRACTION  # 0.80
THRESHOLD_POLICY_VERSION = "spine-drop-v1"

# Floors below which a shrink is noise, not a wipe (a tiny/empty prior book must
# not trip the guard). Named here — not reused private helpers — because these
# checks are ADDITIONAL coverage (total-incl-cash / accounts / currencies) that
# assess_snapshot_ingest does not perform.
_MIN_OLD_TOTAL_USD_K = 100.0
_MIN_OLD_POSITIONS = 8

# Per-item value sanity: value_local ≈ shares × price. Flag only GROSS mismatches
# (a wrong/dropped multiplier), tolerant of FX/rounding noise.
_ITEM_VALUE_REL_TOL = 0.05
_ITEM_VALUE_ABS_TOL = 1.0

# Independent-source checks the spec names but that have no data feed yet (§7).
# Data prerequisites — recorded as unavailable, NEVER as failures.
_UNAVAILABLE_CHECKS: tuple[str, ...] = (
    "item_source_binding:no-independent-broker-signed-source-manifest (spec §2A/§7)",
    "instrument_stable_id:no-ISIN/CUSIP/contract-id-feed (spec §2A)",
    "broker_reported_account_total:no-signed-account-total-feed (spec §2A)",
    "expected_event_set_completeness:no-broker-activity-manifest (spec §2A)",
    # Per-item value reconciliation is value_local = shares × price ×
    # contract_multiplier (spec §2A). We have no per-instrument multiplier feed,
    # so shares × price != value_local is EXPECTED for real rows whose
    # multiplier != 1 (e.g. the Leumi index funds). Per-item is therefore a
    # DIAGNOSTIC, not a hard gate; the reliable value defense is
    # broker_reported_account_total (also unavailable).
    "contract_multiplier:no-per-instrument-multiplier-feed (spec §2A value_local=shares×price×multiplier)",
)

RESULT_PASS = "pass"
RESULT_FAIL = "fail"

# Epsilon for at-or-beyond-threshold float comparisons: a mathematically-exact
# 20% drop must FAIL even though ``old*0.8`` is not representable in binary float
# (defect 1). Scaled by the magnitude so it stays a tiny relative slack.
_DROP_EPS = 1e-9

_NUMERIC_MONEY_FIELDS = (
    "shares",
    "current_price",
    "current_value_local",
    "usd_value_k",
    "avg_price",
)


def _drop_at_or_beyond_threshold(old: float, new: float) -> bool:
    """True when ``(old-new)/old >= _SPINE_DROP_FRACTION`` at the boundary.

    Epsilon-safe: an exact 20% drop is caught despite binary-float rounding of
    ``old * 0.8`` (defect 1). Works for integer counts too.
    """
    if old <= 0:
        return False
    return (old - new) >= _SPINE_DROP_FRACTION * old - _DROP_EPS * max(1.0, abs(old))


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def _parse_positions_strict(raw: Any) -> tuple[list[Any] | None, str | None]:
    """Parse ``positions_json`` distinguishing empty from CORRUPT.

    Returns ``(positions, None)`` on success (a missing/empty payload is an
    empty list, not a failure) or ``(None, reason)`` when a non-empty string
    fails ``json.loads`` or does not parse to a list — an UNPARSEABLE book is a
    FAIL, never a silent empty pass (defect 1).
    """
    if raw is None:
        return [], None
    if isinstance(raw, list):
        return raw, None
    if isinstance(raw, str):
        s = raw.strip()
        if s == "":
            return [], None
        try:
            data = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None, "positions_json is not valid JSON (unparseable book)"
        if not isinstance(data, list):
            return None, "positions_json did not parse to a list (unparseable book)"
        return data, None
    return None, f"positions_json has unexpected type {type(raw).__name__}"


def _parse_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# content hash — the WHOLE normalized snapshot (defect 2)
# ---------------------------------------------------------------------------
def _norm_text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _norm_date_str(v: Any) -> str:
    d = _as_date(v)
    return d.isoformat() if d is not None else _norm_text(v)


def _canon(obj: Any) -> Any:
    """Recursive canonicalization: sorted dict keys + LOSSLESS, type-tagged leaves.

    - dicts: every key kept, sorted, recursively canonicalized (NO allow-list).
    - ints stay ints (``"i:"`` tag) so distinct huge integer share counts
      (9007199254740992 vs …993) never collide via float coercion (defect 3).
    - floats use ``repr`` (lossless round-trip); tagged ``"f:"`` so ``1`` and
      ``1.0`` stay distinct. Non-finite floats keep their repr (``nan``/``inf``)
      — deterministic here; the FINITE gate lives in assess (defect 2).
    - date/datetime → ISO string. Everything else kept verbatim so a change in
      ANY field (incl. ``review_status``, ``avg_price``, ``mark_stale``) changes
      the hash.
    """
    if isinstance(obj, dict):
        return {str(k): _canon(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, (list, tuple)):
        return [_canon(x) for x in obj]
    if isinstance(obj, bool):
        return "b:" + ("1" if obj else "0")
    if isinstance(obj, int):
        return "i:" + str(obj)
    if isinstance(obj, float):
        return "f:" + repr(obj)
    if isinstance(obj, (date, datetime)):
        return "d:" + obj.isoformat()
    return obj


def _canonical_position(p: Any) -> dict[str, Any]:
    """The COMPLETE normalized position dict — every field, no allow-list.

    Two books that differ in ANY position field yield different hashes (defect 3).
    """
    m = _as_mapping(p)
    if m is None:
        return {"__nonmapping__": repr(p)}
    return _canon(dict(m))


def _canonical_payload(
    positions: Sequence[Any] | None,
    *,
    positions_error_marker: str | None = None,
    snapshot_date: Any = None,
    totals: Any = None,
) -> dict[str, Any]:
    if positions_error_marker is not None:
        pos_part: Any = {"__unparseable_positions__": positions_error_marker}
    else:
        rows = [_canonical_position(p) for p in (positions or [])]
        rows.sort(key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))
        pos_part = rows
    return {
        "snapshot_date": _norm_date_str(snapshot_date) if snapshot_date else "",
        "positions": pos_part,
        "totals": _canon(_parse_json_obj(totals)),
    }


def _hash_payload(payload: dict[str, Any]) -> str:
    blob = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_snapshot_content_hash(snapshot: Any) -> str:
    """Stable SHA-256 over the WHOLE normalized snapshot.

    Accepts a snapshot ROW (with ``positions_json`` / ``snapshot_date`` /
    ``totals_json``) — the production path, which commits to every money/
    partition field — or a bare positions ``list`` (positions-only hashing, for
    unit tests). Reordering rows never changes the hash; changing any committed
    field (incl. ``current_value_local``, ``snapshot_date``, ``managed``, the
    totals payload) does.
    """
    if isinstance(snapshot, (list, tuple)):
        return _hash_payload(_canonical_payload(list(snapshot)))
    raw_positions = getattr(snapshot, "positions_json", None)
    positions, perr = _parse_positions_strict(raw_positions)
    marker = None
    if perr is not None:
        # Commit to the exact offending bytes so distinct corrupt books differ.
        marker = raw_positions if isinstance(raw_positions, str) else repr(raw_positions)
    return _hash_payload(
        _canonical_payload(
            positions,
            positions_error_marker=marker,
            snapshot_date=getattr(snapshot, "snapshot_date", None),
            totals=getattr(snapshot, "totals_json", None),
        )
    )


# ---------------------------------------------------------------------------
# assessment
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IntegrityResult:
    """Outcome of :func:`assess_snapshot_integrity`."""

    result: str  # RESULT_PASS | RESULT_FAIL
    reason: str | None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.result == RESULT_PASS


def _snap_sort_key(row: Any) -> tuple:
    sd = _as_date(getattr(row, "snapshot_date", None)) or date.min
    imp = getattr(row, "imported_at", None) or datetime.min
    # Normalise tz-awareness so naive/aware imported_at values stay comparable.
    if isinstance(imp, datetime) and imp.tzinfo is not None:
        imp = imp.replace(tzinfo=None)
    rid = getattr(row, "id", None) or 0
    return (sd, imp, rid)


def _prior_snapshot_row(session: Any, user_id: str, snapshot_row: Any) -> Any | None:
    """The most recent snapshot STRICTLY BEFORE the assessed row (defect 3).

    Ordered by ``(snapshot_date, imported_at, id)``; NEVER a later row (a later
    row as "prior" would hide a real drop on backfill/re-eval).
    """
    from sqlalchemy import select

    from argosy.state.models import PortfolioSnapshotRow

    if session is None:
        return None
    this_key = _snap_sort_key(snapshot_row)
    this_id = getattr(snapshot_row, "id", None)
    rows = session.execute(
        select(PortfolioSnapshotRow).where(PortfolioSnapshotRow.user_id == user_id)
    ).scalars().all()
    prior = None
    prior_key = None
    for row in rows:
        if getattr(row, "id", None) == this_id:
            continue
        k = _snap_sort_key(row)
        if k < this_key and (prior_key is None or k > prior_key):
            prior, prior_key = row, k
    return prior


def _corrupt_number_reason(field_name: str, v: Any) -> str | None:
    """A present money field that is non-numeric OR non-finite is CORRUPT.

    Covers the missing (None/blank) vs corrupt distinction AND the NaN/Inf hole
    (defect 2): NaN comparisons silently evaluate false, so a non-finite money
    field would pass every threshold check — it is a corrupt-typed FAIL here.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return f"{field_name} is a bool ({v!r})"
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            return f"{field_name}={v!r} is non-finite (NaN/Inf)"
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return None  # blank string = missing, not corrupt
        try:
            f = float(s)
        except ValueError:
            return f"{field_name}={v!r} is not numeric"
        if not math.isfinite(f):
            return f"{field_name}={v!r} is non-finite (NaN/Inf)"
        return None
    return f"{field_name} has non-numeric type {type(v).__name__}"


def _assess_corrupt_fields(positions: Sequence[Any]) -> list[str]:
    bad: list[str] = []
    for p in positions:
        m = _as_mapping(p)
        if m is None:
            bad.append(f"position is not a mapping: {type(p).__name__}")
            continue
        sym = _norm_text(m.get("symbol")) or "-"
        for fld in _NUMERIC_MONEY_FIELDS:
            r = _corrupt_number_reason(fld, m.get(fld))
            if r:
                bad.append(f"{sym}: {r}")
    return bad


def _currency_of(p: Any) -> str:
    m = _as_mapping(p) or {}
    return _norm_text(m.get("currency")).upper() or "?"


def _value_by_key(positions: Sequence[Any], key_fn, arg_fn=None) -> dict[str, float]:
    """Sum ``usd_value_k`` per grouping key (account or currency).

    ``key_fn`` maps a position to its group key; ``arg_fn`` (optional) extracts
    the raw value ``key_fn`` expects (e.g. ``location_account_key`` wants the
    location string, so ``arg_fn=_location_of``). Used to VALUE-weight coverage.
    """
    out: dict[str, float] = {}
    for p in positions:
        k = key_fn(arg_fn(p)) if arg_fn is not None else key_fn(p)
        out[k] = out.get(k, 0.0) + position_usd_value_k(p)
    return out


def _assess_coverage_and_total_drop(
    prior_row: Any | None, positions: Sequence[Any]
) -> list[str]:
    """Explicit >=20% total-value (incl. cash) / count / account / currency drops.

    These are ADDITIONAL to assess_snapshot_ingest (which measures named
    securities only and scopes to covered accounts). Here a dropped account, a
    dropped cash currency, and a total-book shrink incl. cash all FAIL (defect 1).
    """
    if prior_row is None:
        return []
    old_positions, perr = _parse_positions_strict(
        getattr(prior_row, "positions_json", None)
    )
    if perr is not None or old_positions is None:
        return []  # a corrupt PRIOR book is not this snapshot's fault
    fails: list[str] = []

    old_total = investable_usd_k(old_positions)
    new_total = investable_usd_k(positions)
    if old_total >= _MIN_OLD_TOTAL_USD_K and _drop_at_or_beyond_threshold(
        old_total, new_total
    ):
        fails.append(
            f"total value drop (incl. cash) {old_total:.1f} -> {new_total:.1f} usd_k "
            f"(>= {_SPINE_DROP_FRACTION:.0%})"
        )

    old_n, new_n = len(old_positions), len(positions)
    if old_n >= _MIN_OLD_POSITIONS and _drop_at_or_beyond_threshold(old_n, new_n):
        fails.append(
            f"position count drop {old_n} -> {new_n} (>= {_SPINE_DROP_FRACTION:.0%})"
        )

    # Account / currency coverage is VALUE-WEIGHTED at the same >=20% policy as
    # the total-value drop (round-3 fix): a legit small closure (<20% of value)
    # PASSES; a >=20% account/currency silently gone FAILS — even if growth
    # elsewhere holds the total flat, because this is measured on the ABSENT
    # members' prior value, not the net total. Mere member-count loss never fails.
    old_acct_val = _value_by_key(old_positions, location_account_key, _location_of)
    new_accts = accounts_covered_from_positions(positions)
    absent_acct_val = sum(
        v for k, v in old_acct_val.items() if k and k not in new_accts
    )
    if old_total >= _MIN_OLD_TOTAL_USD_K and _drop_at_or_beyond_threshold(
        old_total, old_total - absent_acct_val
    ):
        absent = sorted(k for k in old_acct_val if k and k not in new_accts)
        fails.append(
            f"account/location coverage drop: {absent} absent now, worth "
            f"{absent_acct_val:.1f} usd_k (>= {_SPINE_DROP_FRACTION:.0%} of "
            f"prior {old_total:.1f})"
        )

    old_curr_val = _value_by_key(old_positions, _currency_of)
    new_curr = {k for k in _value_by_key(positions, _currency_of) if k}
    absent_curr_val = sum(
        v for k, v in old_curr_val.items() if k and k not in new_curr
    )
    if old_total >= _MIN_OLD_TOTAL_USD_K and _drop_at_or_beyond_threshold(
        old_total, old_total - absent_curr_val
    ):
        absent = sorted(k for k in old_curr_val if k and k not in new_curr)
        fails.append(
            f"currency coverage drop: {absent} absent now, worth "
            f"{absent_curr_val:.1f} usd_k (>= {_SPINE_DROP_FRACTION:.0%} of "
            f"prior {old_total:.1f})"
        )

    return fails


def _assess_item_value_sanity(positions: Sequence[Any]) -> list[str]:
    bad: list[str] = []
    for p in positions:
        m = _as_mapping(p) or {}

        def _num(x: Any) -> float | None:
            if x is None or isinstance(x, bool):
                return None
            try:
                f = float(x)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) else None

        shares = _num(m.get("shares"))
        price = _num(m.get("current_price"))
        vloc = _num(m.get("current_value_local"))
        if shares is None or price is None or vloc is None or shares <= 0:
            continue
        expected = shares * price
        if expected == 0:
            continue
        diff = abs(expected - vloc)
        if diff > _ITEM_VALUE_ABS_TOL and diff / abs(expected) > _ITEM_VALUE_REL_TOL:
            sym = _symbol_of(p) or "-"
            bad.append(
                f"{sym}: shares*price={expected:.2f} != value_local={vloc:.2f} "
                f"({diff / abs(expected):.1%} off)"
            )
    return bad


def assess_snapshot_integrity(
    session: Any, user_id: str, snapshot_row: Any
) -> IntegrityResult:
    """Run the conservation checks over ``snapshot_row`` → pass/fail.

    Reuses ``holding_books`` for every conservation judgement it already owns.
    Records the threshold-policy version and which checks ran/fired (§3, defect 6).
    """
    raw = getattr(snapshot_row, "positions_json", None)
    positions, perr = _parse_positions_strict(raw)

    checks: dict[str, str] = {}
    fired: list[str] = []
    failures: list[str] = []

    def _fail(name: str, msg: str) -> None:
        checks[name] = RESULT_FAIL
        fired.append(name)
        failures.append(msg)

    def _ok(name: str) -> None:
        checks.setdefault(name, RESULT_PASS)

    prior = _prior_snapshot_row(session, user_id, snapshot_row)

    # 0. Unparseable book — a corrupt/incomplete book is a FAIL, not empty pass.
    if perr is not None:
        _fail("unparseable_book", f"unparseable book: {perr}")
        detail = {
            "checks": checks,
            "checks_fired": fired,
            "threshold_policy_version": THRESHOLD_POLICY_VERSION,
            "prior_snapshot_id": getattr(prior, "id", None),
            "unavailable_checks": list(_UNAVAILABLE_CHECKS),
        }
        return IntegrityResult(RESULT_FAIL, "; ".join(failures), detail)
    _ok("unparseable_book")

    # 1. Corrupt-typed money field — a present non-numeric money field is a FAIL.
    corrupt = _assess_corrupt_fields(positions)
    if corrupt:
        _fail("corrupt_typed_field", "corrupt money field(s): " + "; ".join(corrupt))
    else:
        _ok("corrupt_typed_field")

    # 2. Duplicate / ambiguous-blank guard (holding_books conservation).
    try:
        books_consistency_check_positions(positions)
        _ok("conservation_dup_blank")
    except AssertionError as exc:
        _fail("conservation_dup_blank", f"conservation: {exc}")
    except Exception as exc:  # noqa: BLE001 — surfaced as a loud FAIL, not swallowed
        _fail("conservation_dup_blank", f"conservation check error: {exc}")

    # 3. Catastrophic drop — REUSE holding_books.assess_snapshot_ingest (named
    #    securities + covered-account scoping). allow_stale so date ordering
    #    (now committed in the content hash) does not double-report here.
    try:
        assess_snapshot_ingest(
            latest_row=prior,
            new_positions=positions,
            new_snapshot_date=getattr(snapshot_row, "snapshot_date", None),
            allow_stale=True,
        )
        _ok("catastrophic_drop_ingest")
    except SnapshotIngestRejected as exc:
        _fail("catastrophic_drop_ingest", f"{exc.code}: {exc.detail}")

    # 4. Explicit >=20% total-value (incl. cash) / count / account / currency drops.
    #    Every coverage check is recorded pass-by-default, then fired ones flip.
    for name in (
        "total_value_drop",
        "position_count_drop",
        "account_coverage_drop",
        "currency_coverage_drop",
    ):
        _ok(name)
    for cf in _assess_coverage_and_total_drop(prior, positions):
        name = (
            "total_value_drop" if cf.startswith("total value") else
            "position_count_drop" if cf.startswith("position count") else
            "account_coverage_drop" if cf.startswith("account") else
            "currency_coverage_drop" if cf.startswith("currency") else
            "coverage_drop"
        )
        # _ok used setdefault, so override the pass-default with an explicit fail.
        checks[name] = RESULT_FAIL
        fired.append(name)
        failures.append(cf)

    # 5. Total-book degrade/reprice signal. A load error is a loud FAIL.
    try:
        book = load_total_book(
            session, user_id, positions,
            snapshot_date=getattr(snapshot_row, "snapshot_date", None),
        )
        if book.degraded:
            _fail("book_degraded", f"degraded book: {book.degrade_reason}")
        else:
            _ok("book_degraded")
        if book.stale_marks:
            checks["stale_marks"] = ",".join(book.stale_marks)
    except Exception as exc:  # noqa: BLE001 — surfaced loudly as a FAIL verdict
        _fail("book_degraded", f"book load error: {type(exc).__name__}: {exc}")

    # 6. Per-item value reconciliation — DIAGNOSTIC ONLY, never a hard gate.
    # value_local = shares × price × contract_multiplier (spec §2A); with no
    # per-instrument multiplier feed, shares×price != value_local is EXPECTED
    # for real rows whose multiplier != 1 (Leumi index funds etc.). Record any
    # mismatch for a human, but do NOT fail the book on a check we cannot run
    # reliably without the multiplier data — that would false-refuse the whole
    # legitimate book. The reliable value defense (broker-total reconciliation)
    # is a named unavailable prerequisite.
    mismatches = _assess_item_value_sanity(positions)
    if mismatches:
        checks["per_item_value_local"] = f"diagnostic:unreconciled({len(mismatches)})"
    else:
        _ok("per_item_value_local")

    result = RESULT_FAIL if failures else RESULT_PASS
    detail = {
        "checks": checks,
        "checks_fired": fired,
        "threshold_policy_version": THRESHOLD_POLICY_VERSION,
        "position_count": len(positions),
        "prior_snapshot_id": getattr(prior, "id", None),
        "unavailable_checks": list(_UNAVAILABLE_CHECKS),
        "per_item_value_diagnostics": mismatches,
    }
    return IntegrityResult(
        result=result,
        reason="; ".join(failures) if failures else None,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# verdict recording (append + real CAS with rollback)
# ---------------------------------------------------------------------------
class IntegrityHeadRaced(Exception):
    """Raised when the verdict head moved under us (CAS lost); tx is rolled back."""


class CrossTenantVerdict(Exception):
    """Raised when authoring a verdict for a snapshot owned by another user."""


def record_integrity_verdict(session: Any, user_id: str, snapshot_row: Any):
    """Compute hash + assess, append an ``integrity_verdict``, CAS-advance head.

    ONE transaction (defect 5). ``verdict_seq`` = prior max + 1. The head advance
    is a compare-and-swap: ``UPDATE … WHERE snapshot_id=? AND seq=<expected_old>``
    (expected_old = prior max). ``rowcount != 1`` means a concurrent writer
    advanced it — the WHOLE transaction (verdict insert included) is ROLLED BACK
    and :class:`IntegrityHeadRaced` is raised. No orphan verdict is ever left.
    """
    from sqlalchemy import func, select, update

    from argosy.state.models import IntegrityVerdict, IntegrityVerdictHead

    snapshot_id = getattr(snapshot_row, "id", None)
    if snapshot_id is None:
        raise ValueError("record_integrity_verdict: snapshot_row has no id")
    # Defect 4a: you may only author a verdict for YOUR OWN snapshot. A verdict
    # over another tenant's snapshot is a cross-tenant authorship violation.
    snap_owner = getattr(snapshot_row, "user_id", None)
    if snap_owner != user_id:
        raise CrossTenantVerdict(
            f"user_id {user_id!r} may not author a verdict for snapshot "
            f"{snapshot_id} owned by {snap_owner!r}"
        )

    content_hash = compute_snapshot_content_hash(snapshot_row)
    assessment = assess_snapshot_integrity(session, user_id, snapshot_row)

    prior_max = int(
        session.execute(
            select(func.max(IntegrityVerdict.verdict_seq)).where(
                IntegrityVerdict.snapshot_id == snapshot_id
            )
        ).scalar_one_or_none()
        or 0
    )
    next_seq = prior_max + 1

    verdict = IntegrityVerdict(
        user_id=user_id,
        snapshot_id=snapshot_id,
        result=assessment.result,
        snapshot_content_hash=content_hash,
        verdict_seq=next_seq,
        threshold_policy_version=THRESHOLD_POLICY_VERSION,
        reason=assessment.reason,
        detail_json=json.dumps(assessment.detail, ensure_ascii=False),
        authored_at=datetime.now(timezone.utc),
    )
    try:
        session.add(verdict)
        session.flush()  # assign verdict.id

        existing_seq = session.execute(
            select(IntegrityVerdictHead.seq).where(
                IntegrityVerdictHead.snapshot_id == snapshot_id
            )
        ).scalar_one_or_none()

        if existing_seq is None:
            session.add(
                IntegrityVerdictHead(
                    snapshot_id=snapshot_id,
                    current_verdict_id=verdict.id,
                    seq=next_seq,
                )
            )
            session.flush()
        else:
            res = session.execute(
                update(IntegrityVerdictHead)
                .where(
                    IntegrityVerdictHead.snapshot_id == snapshot_id,
                    IntegrityVerdictHead.seq == prior_max,  # CAS: expected-old
                )
                .values(current_verdict_id=verdict.id, seq=next_seq)
            )
            if res.rowcount != 1:
                session.rollback()
                raise IntegrityHeadRaced(
                    f"integrity_verdict_head for snapshot {snapshot_id} moved "
                    f"(expected seq {prior_max}, found {existing_seq}); rolled back"
                )
        session.commit()
    except IntegrityHeadRaced:
        raise
    except Exception:
        session.rollback()
        raise

    log.info(
        "spine.integrity.verdict_recorded",
        snapshot_id=snapshot_id,
        verdict_id=verdict.id,
        result=verdict.result,
        verdict_seq=next_seq,
        content_hash=content_hash[:12],
        threshold_policy_version=THRESHOLD_POLICY_VERSION,
    )
    return verdict


def record_integrity_verdict_if_absent(session: Any, user_id: str, snapshot_row: Any):
    """Record the FIRST verdict for a snapshot exactly-once — constraint-arbitrated.

    :func:`record_integrity_verdict` ALWAYS appends (``max+1``); its CAS prevents
    a lost head update but NOT a double-record. A check-then-``max+1`` seam is
    racy in a way no exception surfaces: a concurrent writer can COMMIT ``seq=1``
    in the window after our absence check but before ``max+1`` reads ``prior_max``;
    the reader then sees ``prior_max=1``, appends ``seq=2`` and advances the head
    normally — leaving a silent duplicate ``[1, 2]``. The AUTOMATIC paths (persist
    hook, backfill) want at-most-once, so this seam does NOT delegate to ``max+1``.

    Instead the DB UNIQUE constraint is the sole arbiter: we CLAIM
    ``verdict_seq = 1`` for this snapshot in ONE transaction —

      1. INSERT the verdict with ``verdict_seq = 1``. ``UNIQUE(snapshot_id,
         verdict_seq)`` lets exactly ONE writer win; a racing second ``seq=1``
         INSERT raises :class:`~sqlalchemy.exc.IntegrityError`.
      2. INSERT the head (PK ``snapshot_id``) pointing at that verdict; a racing
         head insert likewise collides.
      3. commit.

    On ANY :class:`~sqlalchemy.exc.IntegrityError` / :class:`IntegrityHeadRaced`
    (a concurrent writer won the ``seq=1`` claim or created the head) the whole
    transaction is rolled back and we SKIP (return ``None``) — treated as
    already-recorded, never raised out. Two concurrent if-absent calls: exactly
    one wins the ``seq=1`` INSERT + head; the loser collides, rolls back, skips.

    A fast-path absence check short-circuits the common re-run/backfill case, but
    correctness rides on the constraint, not the check. Deliberate re-assessment
    still goes through :func:`record_integrity_verdict` (``max+1`` + CAS), which
    legitimately appends ``seq>=2`` and supersedes — that path is unchanged.

    Returns the newly-recorded ``IntegrityVerdict``, or ``None`` when a verdict
    was already present (or won by a concurrent writer).
    """
    from sqlalchemy.exc import IntegrityError

    from argosy.state.models import IntegrityVerdict, IntegrityVerdictHead

    snapshot_id = getattr(snapshot_row, "id", None)
    if snapshot_id is None:
        raise ValueError("record_integrity_verdict_if_absent: snapshot_row has no id")
    # Cross-tenant authorship is a programming error, NOT a race — raise (mirrors
    # record_integrity_verdict). Never author a verdict over another's snapshot.
    snap_owner = getattr(snapshot_row, "user_id", None)
    if snap_owner != user_id:
        raise CrossTenantVerdict(
            f"user_id {user_id!r} may not author a verdict for snapshot "
            f"{snapshot_id} owned by {snap_owner!r}"
        )

    # Fast path only — the UNIQUE claim below is the real arbiter.
    if session.get(IntegrityVerdictHead, snapshot_id) is not None:
        return None

    content_hash = compute_snapshot_content_hash(snapshot_row)
    assessment = assess_snapshot_integrity(session, user_id, snapshot_row)

    verdict = IntegrityVerdict(
        user_id=user_id,
        snapshot_id=snapshot_id,
        result=assessment.result,
        snapshot_content_hash=content_hash,
        verdict_seq=1,  # the CLAIM — UNIQUE(snapshot_id, verdict_seq) arbitrates
        threshold_policy_version=THRESHOLD_POLICY_VERSION,
        reason=assessment.reason,
        detail_json=json.dumps(assessment.detail, ensure_ascii=False),
        authored_at=datetime.now(timezone.utc),
    )
    try:
        session.add(verdict)
        session.flush()  # UNIQUE(snapshot_id, verdict_seq=1) enforced here
        session.add(
            IntegrityVerdictHead(
                snapshot_id=snapshot_id,
                current_verdict_id=verdict.id,
                seq=1,
            )
        )
        session.flush()  # head PK(snapshot_id) enforced here
        session.commit()
    except (IntegrityError, IntegrityHeadRaced) as exc:
        # A concurrent writer won the seq=1 claim (or created the head) between
        # our absence check and this insert — exactly-once holds via the DB.
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.info(
            "spine.integrity.verdict_already_present",
            snapshot_id=snapshot_id,
            reason=f"{type(exc).__name__}",
        )
        return None
    except Exception:
        session.rollback()
        raise

    log.info(
        "spine.integrity.verdict_recorded",
        snapshot_id=snapshot_id,
        verdict_id=verdict.id,
        result=verdict.result,
        verdict_seq=1,
        content_hash=content_hash[:12],
        threshold_policy_version=THRESHOLD_POLICY_VERSION,
        exactly_once=True,
    )
    return verdict


def backfill_integrity_verdicts(session: Any, user_id: str | None = None) -> dict[str, int]:
    """Record a verdict for every snapshot that has NO current verdict head.

    Idempotent + re-runnable: a snapshot that already carries an
    ``integrity_verdict_head`` is SKIPPED (never re-appended), so a second run
    over the same book is a no-op. Optionally scope to a single ``user_id``;
    otherwise every user's snapshots are covered (each verdict is authored under
    the snapshot's OWN ``user_id``, so cross-tenant authorship never occurs).

    Writes ONLY the spine tables (``integrity_verdict`` / ``integrity_verdict_head``)
    — never the money tables. Each snapshot is recorded in its own transaction
    (``record_integrity_verdict`` commits per row) via the exactly-once
    :func:`record_integrity_verdict_if_absent` seam, so it can safely race a live
    persist hook. A single row's failure — INCLUDING a failed head-existence
    lookup — is logged and does NOT abort the backfill. Returns a
    ``{recorded, skipped, failed, total}`` tally.
    """
    from sqlalchemy import select

    from argosy.state.models import PortfolioSnapshotRow

    q = select(PortfolioSnapshotRow)
    if user_id is not None:
        q = q.where(PortfolioSnapshotRow.user_id == user_id)
    rows = session.execute(q.order_by(PortfolioSnapshotRow.id)).scalars().all()

    recorded = skipped = failed = 0
    for row in rows:
        snapshot_id = getattr(row, "id", None)
        if snapshot_id is None:
            failed += 1
            continue
        try:
            # Head-existence check lives INSIDE the try (via record-if-absent) so
            # a DB error during the lookup counts as one failed row, never an
            # aborted run. record-if-absent returns None when already headed.
            verdict = record_integrity_verdict_if_absent(session, row.user_id, row)
            if verdict is None:
                skipped += 1
            else:
                recorded += 1
        except Exception as exc:  # noqa: BLE001 — one bad row must not abort backfill
            failed += 1
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            log.warning(
                "spine.integrity.backfill_row_failed",
                snapshot_id=snapshot_id,
                user_id=getattr(row, "user_id", None),
                error=f"{type(exc).__name__}: {exc}",
            )

    tally = {
        "recorded": recorded,
        "skipped": skipped,
        "failed": failed,
        "total": len(rows),
    }
    log.info("spine.integrity.backfill_complete", **tally, scoped_user=user_id)
    return tally


__all__ = [
    "IntegrityResult",
    "IntegrityHeadRaced",
    "CrossTenantVerdict",
    "RESULT_PASS",
    "RESULT_FAIL",
    "THRESHOLD_POLICY_VERSION",
    "compute_snapshot_content_hash",
    "assess_snapshot_integrity",
    "record_integrity_verdict",
    "record_integrity_verdict_if_absent",
    "backfill_integrity_verdicts",
]
