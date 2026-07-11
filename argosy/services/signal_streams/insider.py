"""SEC Form 4 insider-cluster early-signal stream.

Qualified buy strength gives equal weight to two saturating components:
distinct-insider count above its configured minimum and aggregate-value
excess above the market-cap-scaled floor. Both components equal 0.5 at
their exact threshold and approach, but never reach, 1.0. Thus an exact
threshold cluster is non-zero while stronger clusters retain headroom.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from argosy.adapters.data.sec_form4_adapter import (
    SecForm4Adapter,
    _is_us_federal_holiday,
)
from argosy.adapters.data.yfinance_adapter import YFinanceAdapter
from argosy.logging import get_logger
from argosy.services.signal_streams.base import SignalNomination
from argosy.state.models import SignalStreamEvent

_log = get_logger("argosy.services.signal_streams.insider")
_C_SUITE_ACRONYM_PATTERN = re.compile(r"\b(?:ceo|cfo|coo|cto|cio|cmo|cro)\b")
_CHIEF_OFFICER_PATTERN = re.compile(r"\bchief(?:\s+[a-z]+){1,5}\s+officer\b")
_PENDING_BUY = 1
_PENDING_WARNING = 2
_PENDING_ALL = _PENDING_BUY | _PENDING_WARNING


@dataclass(frozen=True)
class InsiderClusterConfig:
    lookback_days: int = 14
    recent_scan_days: int = 2
    index_publication_lag_days: int = 2
    daily_pull_days: int = 1
    min_distinct_buyers: int = 2
    min_cluster_value_usd: float = 100_000
    min_cluster_value_market_cap_bps: float = 0.5
    min_distinct_sellers: int = 2
    min_stake_sale_pct: float = 20
    warning_ttl_days: int = 30
    cursor_max_catchup_days: int = 31

    def __post_init__(self) -> None:
        if (
            isinstance(self.lookback_days, bool)
            or not isinstance(self.lookback_days, int)
            or self.lookback_days <= 0
        ):
            raise ValueError("lookback_days must be a positive integer")
        if (
            isinstance(self.recent_scan_days, bool)
            or not isinstance(self.recent_scan_days, int)
            or not 0 < self.recent_scan_days <= self.lookback_days
        ):
            raise ValueError("recent_scan_days must be positive and no greater than lookback_days")
        if (
            isinstance(self.index_publication_lag_days, bool)
            or not isinstance(self.index_publication_lag_days, int)
            or self.index_publication_lag_days < 1
        ):
            raise ValueError(
                "index_publication_lag_days must be an integer of at least 1"
            )
        if self.daily_pull_days != 1 or isinstance(self.daily_pull_days, bool):
            raise ValueError("daily_pull_days must be exactly 1")
        if (
            isinstance(self.min_distinct_buyers, bool)
            or not isinstance(self.min_distinct_buyers, int)
            or self.min_distinct_buyers < 2
        ):
            raise ValueError("min_distinct_buyers must be an integer of at least 2")
        if (
            isinstance(self.min_distinct_sellers, bool)
            or not isinstance(self.min_distinct_sellers, int)
            or self.min_distinct_sellers < 2
        ):
            raise ValueError("min_distinct_sellers must be an integer of at least 2")
        _validate_positive_finite(self.min_cluster_value_usd, "min_cluster_value_usd")
        _validate_nonnegative_finite(
            self.min_cluster_value_market_cap_bps,
            "min_cluster_value_market_cap_bps",
        )
        _validate_positive_finite(self.min_stake_sale_pct, "min_stake_sale_pct")
        if self.min_stake_sale_pct >= 100:
            raise ValueError("min_stake_sale_pct must be less than 100")
        if (
            isinstance(self.warning_ttl_days, bool)
            or not isinstance(self.warning_ttl_days, int)
            or not 0 < self.warning_ttl_days <= 365
        ):
            raise ValueError(
                "warning_ttl_days must be an integer between 1 and 365"
            )
        if (
            isinstance(self.cursor_max_catchup_days, bool)
            or not isinstance(self.cursor_max_catchup_days, int)
            or not 0 < self.cursor_max_catchup_days <= 31
        ):
            raise ValueError(
                "cursor_max_catchup_days must be an integer between 1 and 31"
            )


@dataclass(frozen=True)
class InsiderMarketSnapshot:
    price: float | None
    market_cap: float | None
    average_volume: float | None
    quote_source_url: str


def _validate_positive_finite(value: float, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be positive and finite")


def _validate_nonnegative_finite(value: float, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field} must be non-negative and finite")


def latest_completed_sec_day(today: date, *, lag_days: int) -> date:
    """Return the latest completed SEC index date for a daily run."""
    if isinstance(lag_days, bool) or not isinstance(lag_days, int) or lag_days < 1:
        raise ValueError("lag_days must be an integer of at least 1")
    candidate = today - timedelta(days=lag_days)
    while candidate.weekday() >= 5 or _is_us_federal_holiday(candidate):
        candidate -= timedelta(days=1)
    return candidate


@dataclass(frozen=True)
class _LocalEvent:
    event_key: str
    event_group_key: str
    ticker: str
    event_at: date
    available_at: date
    payload_json: str
    source_urls_json: str
    active: int
    evaluation_pending: int

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise ValueError("signal event payload must be a JSON object")
        return value


@dataclass(frozen=True)
class _OriginalGroup:
    group_key: str
    issuer_cik: str
    owner_keys: tuple[str, ...]
    filed_at: date
    in_lookback: bool


def _complete_owner_identities(row: Mapping[str, Any]) -> tuple[str, ...] | None:
    owners = _reporting_owners(row)
    identities: list[str] = []
    for owner in owners:
        key = _owner_key(owner)
        if key is None:
            return None
        identities.append(f"{key[0]}:{key[1]}")
    return tuple(sorted(set(identities))) or None


def _normalized_issuer_cik(row: Mapping[str, Any]) -> str:
    return str(row.get("issuer_cik") or "").strip().lstrip("0")


def _accession_group_key(value: Any) -> str | None:
    accession = str(value or "").strip()
    if not accession:
        return None
    return accession if accession.startswith("sec-form4:") else f"sec-form4:{accession}"


def _original_group_from_event(
    event: _LocalEvent,
    *,
    window_start: date,
    through: date,
) -> _OriginalGroup | None:
    if event.active != 1:
        return None
    payload = event.payload()
    issuer_cik = str(payload.get("_signal_issuer_cik") or "")
    owner_keys = tuple(
        sorted(str(value) for value in payload.get("_signal_owner_keys", []) if value)
    )
    filed_at = _parse_date(payload.get("_signal_original_filed_date"))
    if not issuer_cik or not owner_keys or filed_at is None:
        return None
    return _OriginalGroup(
        group_key=event.event_group_key,
        issuer_cik=issuer_cik,
        owner_keys=owner_keys,
        filed_at=filed_at,
        in_lookback=(
            window_start <= event.event_at <= through
            and event.available_at <= through
        ),
    )


def _original_group_from_row(
    row: Mapping[str, Any],
    *,
    window_start: date,
    through: date,
) -> _OriginalGroup | None:
    if row.get("is_amendment"):
        return None
    group_key = _accession_group_key(row.get("accession"))
    issuer_cik = _normalized_issuer_cik(row)
    owner_keys = _complete_owner_identities(row)
    filed_at = _parse_date(row.get("filed_at"))
    event_at = _parse_date(row.get("transaction_date"))
    if (
        group_key is None
        or not issuer_cik
        or owner_keys is None
        or filed_at is None
        or event_at is None
    ):
        return None
    return _OriginalGroup(
        group_key=group_key,
        issuer_cik=issuer_cik,
        owner_keys=owner_keys,
        filed_at=filed_at,
        in_lookback=(
            window_start <= event_at <= through
            and filed_at <= through
        ),
    )


def _event_from_row(
    raw_row: Mapping[str, Any],
    *,
    group_key: str,
    original_filed_at: date | None,
    owner_keys: tuple[str, ...] | None,
    issuer_cik: str,
    active: bool,
    resolution_reason: str,
    candidate_groups: list[str],
) -> _LocalEvent | None:
    row = dict(raw_row)
    event_at = _parse_date(row.get("transaction_date"))
    available_at = _parse_date(row.get("filed_at"))
    transaction_index = row.get("transaction_index")
    is_derivative = row.get("is_derivative")
    if (
        event_at is None
        or available_at is None
        or isinstance(transaction_index, bool)
        or not isinstance(transaction_index, int)
        or transaction_index < 0
        or not isinstance(is_derivative, bool)
    ):
        return None
    source_urls = sorted(
        str(url) for url in (row.get("source_urls") or []) if url
    )
    is_amendment = bool(row.get("is_amendment"))
    if is_amendment:
        row["amendment_match_status"] = "matched" if active else "ambiguous"
        row["amendment_ambiguity_evidence"] = list(candidate_groups)
    row.update(
        {
            "filing_identity": group_key,
            "cluster_eligible": bool(active),
            "source_urls": source_urls,
            "_signal_group_key": group_key,
            "_signal_owner_keys": list(owner_keys or ()),
            "_signal_issuer_cik": issuer_cik,
            "_signal_original_filed_date": (
                original_filed_at.isoformat()
                if original_filed_at is not None
                else None
            ),
            "_signal_resolution_reason": resolution_reason,
            "_signal_candidate_groups": list(candidate_groups),
        }
    )
    table = "derivative" if is_derivative else "non_derivative"
    return _LocalEvent(
        event_key=f"{group_key}:{table}:{transaction_index}",
        event_group_key=group_key,
        ticker=str(row.get("ticker") or "").strip().upper(),
        event_at=event_at,
        available_at=available_at,
        payload_json=json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
        source_urls_json=json.dumps(source_urls, separators=(",", ":")),
        active=int(active),
        evaluation_pending=_PENDING_ALL if active else 0,
    )


def _normalize_daily_rows(
    rows: list[dict[str, Any]],
    *,
    existing_events: list[_LocalEvent],
    window_start: date,
    through: date,
) -> tuple[list[_LocalEvent], set[str], set[str]]:
    normalized: list[_LocalEvent] = []
    replacement_groups: set[str] = set()
    tainted_groups: set[str] = set()
    candidates: dict[str, _OriginalGroup] = {}
    for event in existing_events:
        candidate = _original_group_from_event(
            event,
            window_start=window_start,
            through=through,
        )
        if candidate is not None:
            prior = candidates.get(candidate.group_key)
            candidates[candidate.group_key] = (
                replace(candidate, in_lookback=True)
                if prior is not None and prior.in_lookback
                else candidate
            )
    for row in rows:
        candidate = _original_group_from_row(
            row,
            window_start=window_start,
            through=through,
        )
        if candidate is not None:
            candidates[candidate.group_key] = candidate

    original_rows = [row for row in rows if not row.get("is_amendment")]
    amendment_rows = [row for row in rows if row.get("is_amendment")]
    for row in original_rows:
        candidate = _original_group_from_row(
            row,
            window_start=window_start,
            through=through,
        )
        if candidate is None:
            continue
        event = _event_from_row(
            row,
            group_key=candidate.group_key,
            original_filed_at=candidate.filed_at,
            owner_keys=candidate.owner_keys,
            issuer_cik=candidate.issuer_cik,
            active=row.get("cluster_eligible") is True,
            resolution_reason="original_accession",
            candidate_groups=[],
        )
        if event is not None:
            normalized.append(event)
            replacement_groups.add(candidate.group_key)

    for row in amendment_rows:
        issuer_cik = _normalized_issuer_cik(row)
        owner_keys = _complete_owner_identities(row)
        original_filed_at = _parse_date(row.get("date_of_original_submission"))
        adapter_group = None
        if row.get("amendment_match_status") == "matched":
            filing_identity = str(row.get("filing_identity") or "").strip()
            accession = str(row.get("accession") or "").strip()
            if filing_identity and filing_identity != accession:
                adapter_group = _accession_group_key(filing_identity)
                if (
                    adapter_group is not None
                    and issuer_cik
                    and owner_keys is not None
                    and original_filed_at is not None
                ):
                    candidates.setdefault(
                        adapter_group,
                        _OriginalGroup(
                            group_key=adapter_group,
                            issuer_cik=issuer_cik,
                            owner_keys=owner_keys,
                            filed_at=original_filed_at,
                            in_lookback=True,
                        ),
                    )

        exact_candidates = sorted(
            candidate.group_key
            for candidate in candidates.values()
            if issuer_cik
            and owner_keys is not None
            and original_filed_at is not None
            and candidate.issuer_cik == issuer_cik
            and candidate.owner_keys == owner_keys
            and candidate.filed_at == original_filed_at
        )
        resolved_group = exact_candidates[0] if len(exact_candidates) == 1 else None
        if resolved_group is not None:
            resolution_reason = "unique_original_candidate"
            candidate_groups = exact_candidates
            active = True
            group_key = resolved_group
            replacement_groups.add(group_key)
            resolved = candidates[group_key]
            resolved_owners = resolved.owner_keys
            resolved_date = resolved.filed_at
        else:
            if len(exact_candidates) > 1:
                resolution_reason = "multiple_original_candidates"
                candidate_groups = exact_candidates
            elif owner_keys is None and issuer_cik and original_filed_at is not None:
                resolution_reason = "ownerless_original_date_scope"
                candidate_groups = sorted(
                    candidate.group_key
                    for candidate in candidates.values()
                    if candidate.issuer_cik == issuer_cik
                    and candidate.filed_at == original_filed_at
                )
            elif owner_keys is None and issuer_cik:
                resolution_reason = "ownerless_issuer_lookback_scope"
                candidate_groups = sorted(
                    candidate.group_key
                    for candidate in candidates.values()
                    if candidate.issuer_cik == issuer_cik
                    and candidate.in_lookback
                )
            else:
                resolution_reason = "no_original_candidate_overlap_scope"
                candidate_groups = sorted(
                    candidate.group_key
                    for candidate in candidates.values()
                    if candidate.issuer_cik == issuer_cik
                    and owner_keys is not None
                    and set(candidate.owner_keys).intersection(owner_keys)
                )
            tainted_groups.update(candidate_groups)
            group_key = _accession_group_key(row.get("accession"))
            if group_key is None:
                digest = hashlib.sha256(
                    json.dumps(row, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()[:20]
                group_key = f"sec-form4:unidentified-amendment:{digest}"
            active = False
            resolved_owners = owner_keys
            resolved_date = original_filed_at

        event = _event_from_row(
            row,
            group_key=group_key,
            original_filed_at=resolved_date,
            owner_keys=resolved_owners,
            issuer_cik=issuer_cik,
            active=active,
            resolution_reason=resolution_reason,
            candidate_groups=candidate_groups,
        )
        if event is not None:
            normalized.append(event)
    return normalized, replacement_groups, tainted_groups


def market_cap_floor(market_cap: float, *, config: InsiderClusterConfig) -> float:
    """Return the larger of the absolute and market-cap-scaled buy floors."""
    _validate_positive_finite(market_cap, "market_cap")
    scaled = market_cap * config.min_cluster_value_market_cap_bps / 10_000
    return max(float(config.min_cluster_value_usd), scaled)


def cluster_strength(
    *,
    distinct_count: int,
    aggregate_value_usd: float,
    floor_usd: float,
    config: InsiderClusterConfig,
) -> float:
    """Score a qualified buy cluster monotonically on count and value excess."""
    _validate_positive_finite(floor_usd, "floor_usd")
    if distinct_count < config.min_distinct_buyers or aggregate_value_usd < floor_usd:
        return 0.0
    count_excess = distinct_count - config.min_distinct_buyers
    value_excess_ratio = aggregate_value_usd / floor_usd - 1.0
    count_component = 0.5 + 0.5 * (count_excess / (count_excess + config.min_distinct_buyers))
    value_component = 0.5 + 0.5 * (value_excess_ratio / (1.0 + value_excess_ratio))
    return min(1.0, max(0.0, (count_component + value_component) / 2.0))


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _normalised_filer_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _insider_key(row: Mapping[str, Any]) -> tuple[str, str] | None:
    cik = str(row.get("filer_cik") or "").strip().lstrip("0")
    if cik:
        return ("cik", cik)
    name = _normalised_filer_name(row.get("filer_name"))
    return ("name", name) if name else None


def _reporting_owners(
    row: Mapping[str, Any],
) -> list[dict[str, str]]:
    owners = row.get("reporting_owners")
    if isinstance(owners, list):
        valid = [
            {
                "filer_cik": str(owner.get("filer_cik") or ""),
                "filer_name": str(owner.get("filer_name") or ""),
                "role": str(owner.get("role") or "unknown"),
            }
            for owner in owners
            if isinstance(owner, Mapping)
        ]
        if valid:
            return valid
    return [
        {
            "filer_cik": str(row.get("filer_cik") or ""),
            "filer_name": str(row.get("filer_name") or ""),
            "role": str(row.get("role") or "unknown"),
        }
    ]


def _owner_key(owner: Mapping[str, Any]) -> tuple[str, str] | None:
    cik = str(owner.get("filer_cik") or "").strip().lstrip("0")
    if cik:
        return ("cik", cik)
    name = _normalised_filer_name(owner.get("filer_name"))
    return ("name", name) if name else None


def _qualifying_owner_keys(
    row: Mapping[str, Any],
    *,
    role_predicate: Callable[[str], bool],
) -> set[tuple[str, str]]:
    owners = _reporting_owners(row)
    if not any(role_predicate(owner["role"]) for owner in owners):
        return set()
    owner_keys = sorted(
        {
            f"{kind}:{value}"
            for owner in owners
            if (key := _owner_key(owner)) is not None
            for kind, value in [key]
        }
    )
    if not owner_keys:
        return set()
    return {("owners", "|".join(owner_keys))}


def _transaction_identity(row: Mapping[str, Any]) -> str | None:
    filing_identity = str(row.get("filing_identity") or "").strip()
    index = row.get("transaction_index")
    is_derivative = row.get("is_derivative")
    if (
        not filing_identity
        or isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or not isinstance(is_derivative, bool)
    ):
        return None
    table = "derivative" if is_derivative else "non_derivative"
    return f"{filing_identity}:{table}:{index}"


def _row_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("transaction_date") or ""),
        str(row.get("accession") or ""),
        str(row.get("transaction_index") or 0),
    )


def _deduplicate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical_rows = sorted(
        rows,
        key=lambda row: json.dumps(row, sort_keys=True, default=str),
    )
    by_identity: dict[str, dict[str, Any]] = {}
    for row in canonical_rows:
        identity = _transaction_identity(row)
        if identity is not None:
            by_identity.setdefault(identity, row)
    return sorted(by_identity.values(), key=_row_sort_key)


def _base_eligible(
    row: Mapping[str, Any],
    *,
    window_start: date,
    through: date,
) -> bool:
    transaction_date = _parse_date(row.get("transaction_date"))
    filed_at = _parse_date(row.get("filed_at"))
    return bool(
        transaction_date is not None
        and filed_at is not None
        and transaction_date <= filed_at
        and window_start <= transaction_date <= through
        and _transaction_identity(row) is not None
        and _insider_key(row) is not None
        and row.get("cluster_eligible") is True
        and row.get("is_derivative") is False
        and row.get("is_10b5_1") is False
    )


def _role_is_officer_or_director(role: str) -> bool:
    role = role.casefold()
    return "officer" in role or "director" in role


def _is_officer_or_director(row: Mapping[str, Any]) -> bool:
    return any(
        _role_is_officer_or_director(owner["role"])
        for owner in _reporting_owners(row)
    )


def _role_is_c_suite(role: str) -> bool:
    role = role.casefold()
    officer_title = re.search(r"\bofficer\s*\(([^)]*)\)", role)
    title = officer_title.group(1) if officer_title else role
    normalized = re.sub(r"[^a-z0-9]+", " ", title).strip()
    if _C_SUITE_ACRONYM_PATTERN.search(normalized):
        return True
    if _CHIEF_OFFICER_PATTERN.search(normalized):
        return True
    return normalized == "president"


def _is_c_suite(row: Mapping[str, Any]) -> bool:
    return _role_is_c_suite(str(row.get("role") or ""))


def _valid_transaction_value(row: Mapping[str, Any]) -> float | None:
    shares = _positive_number(row.get("shares"))
    price = _positive_number(row.get("price_per_share"))
    value = _positive_number(row.get("value_usd"))
    if shares is None or price is None or value is None:
        return None
    return value


def _normalise_pool_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _ownership_pool(
    row: Mapping[str, Any],
) -> tuple[tuple[str, str, str, str], dict[str, str]]:
    security_title = _normalise_pool_text(row.get("security_title"))
    direct_or_indirect = str(row.get("direct_or_indirect_ownership") or "").strip().upper()
    nature = _normalise_pool_text(row.get("nature_of_ownership"))
    transaction_table = "derivative" if row.get("is_derivative") is True else "non_derivative"
    evidence = {
        "security_title": security_title,
        "direct_or_indirect_ownership": direct_or_indirect,
        "nature_of_ownership": nature,
        "transaction_table": transaction_table,
    }
    return (
        security_title,
        direct_or_indirect,
        nature,
        transaction_table,
    ), evidence


def _evidence_transaction(
    row: Mapping[str, Any],
    *,
    holder_stake_sale_pct: float | None = None,
    ownership_pool: dict[str, str] | None = None,
) -> dict[str, Any]:
    transaction_date = _parse_date(row.get("transaction_date"))
    filed_at = _parse_date(row.get("filed_at"))
    evidence = {
        "accession": str(row.get("accession") or ""),
        "filing_identity": str(row.get("filing_identity") or ""),
        "transaction_index": row.get("transaction_index"),
        "filer_cik": str(row.get("filer_cik") or ""),
        "filer_name": str(row.get("filer_name") or ""),
        "role": str(row.get("role") or ""),
        "reporting_owners": [
            dict(owner) for owner in _reporting_owners(row)
        ],
        "transaction_date": transaction_date.isoformat() if transaction_date else "",
        "filed_at": filed_at.isoformat() if filed_at else "",
        "filing_lag_days": (
            (filed_at - transaction_date).days
            if filed_at is not None and transaction_date is not None
            else None
        ),
        "transaction_code": str(row.get("transaction_code") or ""),
        "acquired_disposed_code": row.get("acquired_disposed_code"),
        "shares": row.get("shares"),
        "price_per_share": row.get("price_per_share"),
        "value_usd": row.get("value_usd"),
        "post_transaction_holdings": row.get("post_transaction_holdings"),
        "security_title": str(row.get("security_title") or ""),
        "direct_or_indirect_ownership": str(row.get("direct_or_indirect_ownership") or ""),
        "nature_of_ownership": str(row.get("nature_of_ownership") or ""),
        "is_derivative": row.get("is_derivative"),
        "transaction_table": (
            "derivative" if row.get("is_derivative") is True else "non_derivative"
        ),
        "is_10b5_1": row.get("is_10b5_1"),
        "tenb5_1_evidence": list(row.get("tenb5_1_evidence") or []),
        "document_has_10b5_1": row.get("document_has_10b5_1"),
        "document_10b5_1_evidence": list(row.get("document_10b5_1_evidence") or []),
        "is_amendment": bool(row.get("is_amendment")),
        "document_type": str(row.get("document_type") or ""),
        "amendment_match_status": str(row.get("amendment_match_status") or ""),
        "amendment_ambiguity_evidence": list(row.get("amendment_ambiguity_evidence") or []),
        "cluster_eligible": row.get("cluster_eligible"),
        "source_urls": sorted(str(url) for url in (row.get("source_urls") or []) if url),
    }
    if holder_stake_sale_pct is not None:
        evidence["stake_sale_pct"] = holder_stake_sale_pct
        evidence["holder_aggregate_stake_sale_pct"] = holder_stake_sale_pct
    if ownership_pool is not None:
        evidence["ownership_pool"] = dict(ownership_pool)
    return evidence


def _dedup_key(*, ticker: str, direction: str, transactions: list[dict[str, Any]]) -> str:
    identities = sorted(
        {str(transaction["filing_identity"]) for transaction in transactions}
    )
    digest = hashlib.sha256(
        json.dumps(identities, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sec-form4:{direction}:{ticker}:{digest}"


def _base_cluster_evidence(
    *,
    transactions: list[dict[str, Any]],
    distinct_count: int,
    aggregate_value_usd: float,
    snapshot: InsiderMarketSnapshot,
    config: InsiderClusterConfig,
) -> dict[str, Any]:
    filed_dates = [
        parsed
        for transaction in transactions
        if (parsed := _parse_date(transaction["filed_at"])) is not None
    ]
    lags = [
        int(transaction["filing_lag_days"])
        for transaction in transactions
        if transaction["filing_lag_days"] is not None
    ]
    return {
        "transactions": transactions,
        "distinct_insider_count": distinct_count,
        "aggregate_value_usd": aggregate_value_usd,
        "lookback_days": config.lookback_days,
        "market_cap": snapshot.market_cap,
        "price": snapshot.price,
        "average_volume": snapshot.average_volume,
        "quote_source_url": snapshot.quote_source_url,
        "latest_filed_date": max(filed_dates).isoformat() if filed_dates else None,
        "max_filing_lag_days": max(lags) if lags else None,
    }


def _buy_cluster_inputs(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, float]:
    eligible = [
        row
        for row in rows
        if str(row.get("transaction_code") or "").upper() == "P"
        and str(row.get("acquired_disposed_code") or "").upper() in {"", "A"}
        and _is_officer_or_director(row)
        and _valid_transaction_value(row) is not None
    ]
    insiders = {
        insider
        for row in eligible
        for insider in _qualifying_owner_keys(
            row,
            role_predicate=_role_is_officer_or_director,
        )
    }
    aggregate = sum(_valid_transaction_value(row) or 0.0 for row in eligible)
    return eligible, len(insiders), aggregate


def _buy_nomination(
    ticker: str,
    rows: list[dict[str, Any]],
    *,
    snapshot: InsiderMarketSnapshot,
    config: InsiderClusterConfig,
) -> SignalNomination | None:
    eligible, distinct_count, aggregate = _buy_cluster_inputs(rows)
    if distinct_count < config.min_distinct_buyers:
        return None
    floor = market_cap_floor(float(snapshot.market_cap), config=config)
    if aggregate < floor:
        return None
    transactions = [_evidence_transaction(row) for row in eligible]
    evidence = _base_cluster_evidence(
        transactions=transactions,
        distinct_count=distinct_count,
        aggregate_value_usd=aggregate,
        snapshot=snapshot,
        config=config,
    )
    scaled = float(snapshot.market_cap) * config.min_cluster_value_market_cap_bps / 10_000
    evidence["threshold"] = {
        "base_usd": float(config.min_cluster_value_usd),
        "market_cap_bps": float(config.min_cluster_value_market_cap_bps),
        "market_cap_scaled_usd": scaled,
        "effective_floor_usd": floor,
        "derivation": "max(base_usd, market_cap * market_cap_bps / 10000)",
    }
    return SignalNomination(
        ticker=ticker,
        stream="insider_cluster",
        direction="long",
        strength=cluster_strength(
            distinct_count=distinct_count,
            aggregate_value_usd=aggregate,
            floor_usd=floor,
            config=config,
        ),
        as_of=max(
            parsed for row in eligible if (parsed := _parse_date(row.get("filed_at"))) is not None
        ),
        evidence=evidence,
        dedup_key=_dedup_key(ticker=ticker, direction="long", transactions=transactions),
    )


def _warning_strength(
    *, distinct_count: int, stake_pcts: list[float], config: InsiderClusterConfig
) -> float:
    count_excess = distinct_count - config.min_distinct_sellers
    count_component = 0.5 + 0.5 * (count_excess / (count_excess + config.min_distinct_sellers))
    average_ratio = sum(stake_pcts) / len(stake_pcts) / config.min_stake_sale_pct
    stake_excess = average_ratio - 1.0
    stake_component = 0.5 + 0.5 * (stake_excess / (1.0 + stake_excess))
    return min(1.0, max(0.0, (count_component + stake_component) / 2.0))


def _sell_nomination(
    ticker: str,
    rows: list[dict[str, Any]],
    *,
    snapshot: InsiderMarketSnapshot,
    config: InsiderClusterConfig,
) -> SignalNomination | None:
    rows_by_seller_pool: dict[
        tuple[tuple[str, str], tuple[str, str, str, str]],
        list[dict[str, Any]],
    ] = {}
    for row in rows:
        owners = _reporting_owners(row)
        if len(owners) != 1:
            # Transaction-to-owner stake attribution is ambiguous in a
            # joint filing, so sell warnings fail closed.
            continue
        insider = _owner_key(owners[0])
        if (
            insider is not None
            and str(row.get("transaction_code") or "").upper() == "S"
            and _role_is_c_suite(owners[0]["role"])
        ):
            pool_key, _ = _ownership_pool(row)
            rows_by_seller_pool.setdefault((insider, pool_key), []).append(row)

    qualifying_pools: list[
        tuple[
            tuple[str, str],
            dict[str, str],
            list[dict[str, Any]],
            dict[str, Any],
        ]
    ] = []
    for (insider, _), pool_rows in sorted(rows_by_seller_pool.items()):
        shares = [_positive_number(row.get("shares")) for row in pool_rows]
        post_holdings = [
            _nonnegative_number(row.get("post_transaction_holdings")) for row in pool_rows
        ]
        values = [_valid_transaction_value(row) for row in pool_rows]
        if (
            any(value is None for value in shares)
            or any(value is None for value in post_holdings)
            or any(value is None for value in values)
        ):
            continue
        total_shares = sum(float(value) for value in shares if value is not None)
        minimum_post_holdings = min(float(value) for value in post_holdings if value is not None)
        pre_sale_stake = minimum_post_holdings + total_shares
        stake_pct = total_shares / pre_sale_stake * 100
        if stake_pct <= config.min_stake_sale_pct:
            continue
        aggregate_value = sum(float(value) for value in values if value is not None)
        representative = pool_rows[0]
        _, pool_evidence = _ownership_pool(representative)
        qualifying_pools.append(
            (
                insider,
                pool_evidence,
                sorted(pool_rows, key=_row_sort_key),
                {
                    "filer_cik": str(representative.get("filer_cik") or ""),
                    "filer_name": str(representative.get("filer_name") or ""),
                    "role": str(representative.get("role") or ""),
                    "ownership_pool": pool_evidence,
                    "transaction_count": len(pool_rows),
                    "total_shares_sold": total_shares,
                    "minimum_post_transaction_holdings": minimum_post_holdings,
                    "pre_sale_stake": pre_sale_stake,
                    "stake_sale_pct": stake_pct,
                    "aggregate_value_usd": aggregate_value,
                },
            )
        )

    distinct_count = len({insider for insider, _, _, _ in qualifying_pools})
    if distinct_count < config.min_distinct_sellers:
        return None
    aggregate = sum(
        float(holder_evidence["aggregate_value_usd"])
        for _, _, _, holder_evidence in qualifying_pools
    )
    transactions = [
        _evidence_transaction(
            row,
            holder_stake_sale_pct=float(holder_evidence["stake_sale_pct"]),
            ownership_pool=pool_evidence,
        )
        for _, pool_evidence, pool_rows, holder_evidence in qualifying_pools
        for row in pool_rows
    ]
    seller_pools = [holder_evidence for _, _, _, holder_evidence in qualifying_pools]
    stake_pct_by_seller: dict[tuple[str, str], float] = {}
    for insider, _, _, holder_evidence in qualifying_pools:
        stake_pct_by_seller[insider] = max(
            stake_pct_by_seller.get(insider, 0.0),
            float(holder_evidence["stake_sale_pct"]),
        )
    evidence = _base_cluster_evidence(
        transactions=transactions,
        distinct_count=distinct_count,
        aggregate_value_usd=aggregate,
        snapshot=snapshot,
        config=config,
    )
    evidence.update(
        {
            "warning_only": True,
            "stake_sale_threshold_pct": config.min_stake_sale_pct,
            "stake_sale_threshold_comparison": "strictly_greater_than",
            "seller_aggregates": seller_pools,
            "seller_pools": seller_pools,
        }
    )
    return SignalNomination(
        ticker=ticker,
        stream="insider_cluster",
        direction="short",
        strength=_warning_strength(
            distinct_count=distinct_count,
            stake_pcts=list(stake_pct_by_seller.values()),
            config=config,
        ),
        as_of=max(
            parsed
            for _, _, pool_rows, _ in qualifying_pools
            for row in pool_rows
            if (parsed := _parse_date(row.get("filed_at"))) is not None
        ),
        evidence=evidence,
        dedup_key=_dedup_key(ticker=ticker, direction="short", transactions=transactions),
        route_to_funnel=False,
    )


def _nomination_transaction_identities(
    nomination: SignalNomination,
) -> frozenset[str]:
    identities: set[str] = set()
    for transaction in nomination.evidence.get("transactions", []):
        filing_identity = str(transaction.get("filing_identity") or "")
        transaction_table = str(transaction.get("transaction_table") or "")
        transaction_index = transaction.get("transaction_index")
        if filing_identity and transaction_table and transaction_index is not None:
            identities.add(
                f"{filing_identity}:{transaction_table}:{transaction_index}"
            )
    return frozenset(identities)


def _collapse_overlapping_nominations(
    nominations: list[SignalNomination],
) -> list[SignalNomination]:
    unique = {nomination.dedup_key: nomination for nomination in nominations}
    remaining = sorted(unique.values(), key=lambda item: item.dedup_key)
    representatives: list[SignalNomination] = []
    while remaining:
        component = [remaining.pop(0)]
        component_identities = set(
            _nomination_transaction_identities(component[0])
        )
        changed = True
        while changed:
            changed = False
            still_remaining: list[SignalNomination] = []
            for candidate in remaining:
                identities = _nomination_transaction_identities(candidate)
                if component_identities.intersection(identities):
                    component.append(candidate)
                    component_identities.update(identities)
                    changed = True
                else:
                    still_remaining.append(candidate)
            remaining = still_remaining
        representatives.append(
            max(
                component,
                key=lambda item: (
                    item.as_of,
                    item.strength,
                    len(_nomination_transaction_identities(item)),
                    item.dedup_key,
                ),
            )
        )
    return representatives


@dataclass(frozen=True)
class InsiderWindowCandidate:
    rows: tuple[dict[str, Any], ...]
    transaction_identities: frozenset[str]
    window_start: date
    window_end: date
    latest_filed_at: date


@dataclass(frozen=True)
class PreparedInsiderWindows:
    windows_by_ticker: dict[str, tuple[InsiderWindowCandidate, ...]]
    availability_since: date
    through: date
    lookback_days: int


def prepare_insider_windows(
    rows: list[dict[str, Any]],
    *,
    config: InsiderClusterConfig,
    through: date,
    availability_since: date,
) -> PreparedInsiderWindows:
    """Prepare deterministic eligible rolling windows exactly once."""
    predecessor_start = availability_since - timedelta(
        days=config.lookback_days - 1
    )
    grouped: dict[
        str,
        list[tuple[date, date, str, dict[str, Any]]],
    ] = {}
    for row in _deduplicate_rows(rows):
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker and _base_eligible(
            row,
            window_start=predecessor_start,
            through=through,
        ):
            transaction_date = _parse_date(row.get("transaction_date"))
            filed_at = _parse_date(row.get("filed_at"))
            identity = _transaction_identity(row)
            assert transaction_date is not None
            assert filed_at is not None
            assert identity is not None
            grouped.setdefault(ticker, []).append(
                (transaction_date, filed_at, identity, row)
            )

    windows_by_ticker: dict[str, tuple[InsiderWindowCandidate, ...]] = {}
    for ticker in sorted(grouped):
        entries = sorted(
            grouped[ticker],
            key=lambda item: (item[0], _row_sort_key(item[3])),
        )
        windows: list[InsiderWindowCandidate] = []
        left = 0
        right = 0
        while right < len(entries):
            window_end = entries[right][0]
            end_exclusive = right + 1
            while (
                end_exclusive < len(entries)
                and entries[end_exclusive][0] == window_end
            ):
                end_exclusive += 1
            window_start = window_end - timedelta(
                days=config.lookback_days - 1
            )
            while left < end_exclusive and entries[left][0] < window_start:
                left += 1
            window_entries = entries[left:end_exclusive]
            latest_filed_at = max(item[1] for item in window_entries)
            if latest_filed_at >= availability_since:
                windows.append(
                    InsiderWindowCandidate(
                        rows=tuple(item[3] for item in window_entries),
                        transaction_identities=frozenset(
                            item[2] for item in window_entries
                        ),
                        window_start=window_start,
                        window_end=window_end,
                        latest_filed_at=latest_filed_at,
                    )
                )
            right = end_exclusive
        if windows:
            windows_by_ticker[ticker] = tuple(windows)
    return PreparedInsiderWindows(
        windows_by_ticker=windows_by_ticker,
        availability_since=availability_since,
        through=through,
        lookback_days=config.lookback_days,
    )


_PREFILTER_SNAPSHOT = InsiderMarketSnapshot(
    price=1.0,
    market_cap=1.0,
    average_volume=1.0,
    quote_source_url="prefilter://non-market",
)


def _potential_tickers_from_prepared(
    prepared: PreparedInsiderWindows,
    *,
    config: InsiderClusterConfig,
) -> list[str]:
    groups = _potential_groups_from_prepared(prepared, config=config)
    return sorted(
        ticker
        for ticker, by_direction in groups.items()
        if by_direction["long"] or by_direction["short"]
    )


def _potential_groups_from_prepared(
    prepared: PreparedInsiderWindows,
    *,
    config: InsiderClusterConfig,
) -> dict[str, dict[str, set[str]]]:
    potential: dict[str, dict[str, set[str]]] = {}
    for ticker, windows in prepared.windows_by_ticker.items():
        by_direction = {"long": set(), "short": set()}
        for window in windows:
            window_rows = list(window.rows)
            buy_rows, distinct_buyers, aggregate_buy_value = _buy_cluster_inputs(
                window_rows
            )
            if (
                distinct_buyers >= config.min_distinct_buyers
                and aggregate_buy_value >= config.min_cluster_value_usd
            ):
                by_direction["long"].update(
                    str(row.get("filing_identity") or "")
                    for row in buy_rows
                    if row.get("filing_identity")
                )
            warning = _sell_nomination(
                ticker,
                window_rows,
                snapshot=_PREFILTER_SNAPSHOT,
                config=config,
            )
            if warning is not None:
                by_direction["short"].update(
                    str(transaction.get("filing_identity") or "")
                    for transaction in warning.evidence.get("transactions", [])
                    if transaction.get("filing_identity")
                )
        potential[ticker] = by_direction
    return potential


def potential_insider_nomination_tickers(
    rows: list[dict[str, Any]],
    *,
    config: InsiderClusterConfig,
    through: date,
    availability_since: date,
) -> list[str]:
    """Return tickers that can qualify before current market data is known."""
    prepared = prepare_insider_windows(
        rows,
        config=config,
        through=through,
        availability_since=availability_since,
    )
    return _potential_tickers_from_prepared(prepared, config=config)


def classify_prepared_insider_windows(
    prepared: PreparedInsiderWindows,
    *,
    snapshots: Mapping[str, InsiderMarketSnapshot],
    config: InsiderClusterConfig,
) -> list[SignalNomination]:
    """Apply market-dependent classification to prepared windows."""
    availability_since = prepared.availability_since
    nominations: list[SignalNomination] = []
    for ticker, windows in prepared.windows_by_ticker.items():
        snapshot = snapshots.get(ticker)
        if snapshot is None or _positive_number(snapshot.price) is None:
            continue
        candidates: dict[str, list[SignalNomination]] = {
            "long": [],
            "short": [],
        }
        for window in windows:
            window_rows = list(window.rows)
            window_nominations: list[SignalNomination | None] = []
            if _positive_number(snapshot.market_cap) is not None:
                window_nominations.append(
                    _buy_nomination(
                        ticker,
                        window_rows,
                        snapshot=snapshot,
                        config=config,
                    )
                )
            window_nominations.append(
                _sell_nomination(
                    ticker,
                    window_rows,
                    snapshot=snapshot,
                    config=config,
                )
            )
            for nomination in window_nominations:
                if (
                    nomination is None
                    or nomination.as_of < availability_since
                ):
                    continue
                candidates[nomination.direction].append(nomination)
        ticker_nominations = [
            representative
            for direction in ("long", "short")
            for representative in _collapse_overlapping_nominations(
                candidates[direction]
            )
        ]
        nominations.extend(
            sorted(
                ticker_nominations,
                key=lambda item: (
                    item.as_of,
                    item.direction,
                    item.dedup_key,
                ),
            )
        )
    return nominations


def classify_insider_transactions(
    rows: list[dict[str, Any]],
    *,
    snapshots: Mapping[str, InsiderMarketSnapshot],
    config: InsiderClusterConfig,
    through: date,
    availability_since: date | None = None,
) -> list[SignalNomination]:
    """Purely classify verified Form 4 rows into deterministic clusters."""
    availability_since = availability_since or (
        through - timedelta(days=config.lookback_days - 1)
    )
    prepared = prepare_insider_windows(
        rows,
        config=config,
        through=through,
        availability_since=availability_since,
    )
    return classify_prepared_insider_windows(
        prepared,
        snapshots=snapshots,
        config=config,
    )


class YFinanceInsiderMarketProvider:
    """Synchronous seam over Argosy's async yfinance adapter."""

    def __init__(self, *, adapter: Any | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ticker: str) -> InsiderMarketSnapshot:
        adapter = self._adapter or YFinanceAdapter()
        payload = asyncio.run(adapter.get_quote_with_fundamentals(ticker))
        return InsiderMarketSnapshot(
            price=payload.get("price"),
            market_cap=payload.get("market_cap"),
            average_volume=payload.get("average_volume"),
            quote_source_url=f"https://finance.yahoo.com/quote/{ticker}",
        )


class InsiderClusterStream:
    """Pull one completed SEC day and classify from the local raw-event ledger."""

    name = "insider_cluster"
    cursor_controls_fetch_range = False

    def __init__(
        self,
        *,
        user_id: str = "ariel",
        config: InsiderClusterConfig | None = None,
        sec_adapter: Any | None = None,
        market_snapshot: Callable[[str], InsiderMarketSnapshot] | None = None,
        today: Callable[[], date] = date.today,
        observed_at: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.user_id = user_id
        self.config = config or InsiderClusterConfig()
        self.sec_adapter = sec_adapter or SecForm4Adapter()
        self.market_snapshot = market_snapshot or YFinanceInsiderMarketProvider()
        self.today = today
        self.observed_at = observed_at

    def fetch(self, session: Any, *, since: date) -> list[SignalNomination]:
        del since
        through = latest_completed_sec_day(
            self.today(),
            lag_days=self.config.index_publication_lag_days,
        )
        window_start = through - timedelta(days=self.config.lookback_days - 1)
        existing_rows = (
            list(
                session.execute(
                    select(SignalStreamEvent)
                    .where(SignalStreamEvent.user_id == self.user_id)
                    .where(SignalStreamEvent.stream == self.name)
                    .order_by(SignalStreamEvent.id)
                ).scalars()
            )
            if session is not None
            else []
        )
        existing_by_key = {row.event_key: row for row in existing_rows}
        working: dict[str, _LocalEvent] = {
            row.event_key: _LocalEvent(
                event_key=row.event_key,
                event_group_key=row.event_group_key,
                ticker=row.ticker,
                event_at=row.event_at,
                available_at=row.available_at,
                payload_json=row.payload_json,
                source_urls_json=row.source_urls_json,
                active=row.active,
                evaluation_pending=row.evaluation_pending,
            )
            for row in existing_rows
        }

        fetched_rows = asyncio.run(
            self.sec_adapter.get_form4_for_date_range(
                through,
                through,
            )
        )
        incoming, replacement_groups, tainted_groups = _normalize_daily_rows(
            fetched_rows,
            existing_events=list(working.values()),
            window_start=window_start,
            through=through,
        )
        incoming_by_group: dict[str, set[str]] = {}
        for event in incoming:
            incoming_by_group.setdefault(event.event_group_key, set()).add(
                event.event_key
            )

        for group_key in replacement_groups:
            current_keys = incoming_by_group.get(group_key, set())
            for event_key, event in list(working.items()):
                if (
                    event.event_group_key == group_key
                    and event.active == 1
                    and event_key not in current_keys
                ):
                    working[event_key] = replace(
                        event,
                        active=0,
                        evaluation_pending=0,
                    )

        incoming_keys: set[str] = set()
        for event in incoming:
            incoming_keys.add(event.event_key)
            prior = working.get(event.event_key)
            if prior is not None:
                prior_urls = json.loads(prior.source_urls_json)
                current_urls = json.loads(event.source_urls_json)
                merged_urls = sorted(
                    {
                        str(url)
                        for url in [*prior_urls, *current_urls]
                        if url
                    }
                )
                payload = event.payload()
                payload["source_urls"] = merged_urls
                event = replace(
                    event,
                    payload_json=json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ),
                    source_urls_json=json.dumps(
                        merged_urls,
                        separators=(",", ":"),
                    ),
                )
            prior_content = (
                (
                    prior.event_group_key,
                    prior.ticker,
                    prior.event_at,
                    prior.available_at,
                    prior.payload_json,
                    prior.source_urls_json,
                    prior.active,
                )
                if prior is not None
                else None
            )
            event_content = (
                event.event_group_key,
                event.ticker,
                event.event_at,
                event.available_at,
                event.payload_json,
                event.source_urls_json,
                event.active,
            )
            if prior_content == event_content and prior is not None:
                event = replace(
                    event,
                    evaluation_pending=prior.evaluation_pending,
                )
            elif event.active == 1:
                event = replace(event, evaluation_pending=_PENDING_ALL)
            else:
                event = replace(event, evaluation_pending=0)
            working[event.event_key] = event

        for event_key, event in list(working.items()):
            if event.active == 1 and event.event_group_key in tainted_groups:
                working[event_key] = replace(
                    event,
                    active=0,
                    evaluation_pending=0,
                )

        active_rows = [
            event.payload()
            for event in working.values()
            if event.active == 1
            and window_start <= event.event_at <= through
            and event.available_at <= through
        ]
        prepared = prepare_insider_windows(
            active_rows,
            config=self.config,
            through=through,
            availability_since=window_start,
        )
        potential_groups = _potential_groups_from_prepared(
            prepared,
            config=self.config,
        )
        applicable_masks: dict[str, int] = {}
        for by_direction in potential_groups.values():
            for group_key in by_direction["long"]:
                applicable_masks[group_key] = (
                    applicable_masks.get(group_key, 0) | _PENDING_BUY
                )
            for group_key in by_direction["short"]:
                applicable_masks[group_key] = (
                    applicable_masks.get(group_key, 0) | _PENDING_WARNING
                )
        for event_key, event in list(working.items()):
            if event.active == 1 and event.evaluation_pending:
                working[event_key] = replace(
                    event,
                    evaluation_pending=(
                        event.evaluation_pending
                        & applicable_masks.get(event.event_group_key, 0)
                    ),
                )

        buy_evaluation_groups = {
            event.event_group_key
            for event in working.values()
            if event.active == 1
            and event.evaluation_pending & _PENDING_BUY
        }
        warning_evaluation_groups = {
            event.event_group_key
            for event in working.values()
            if event.active == 1
            and event.evaluation_pending & _PENDING_WARNING
        }
        evaluation_groups = buy_evaluation_groups | warning_evaluation_groups
        evaluation_tickers = {
            event.ticker
            for event in working.values()
            if event.active == 1
            and event.event_group_key in evaluation_groups
        }
        snapshots: dict[str, InsiderMarketSnapshot] = {}
        clear_masks: dict[str, int] = {}
        potential_tickers = {
            ticker
            for ticker, by_direction in potential_groups.items()
            if by_direction["long"] or by_direction["short"]
        }
        for ticker in sorted(potential_tickers.intersection(evaluation_tickers)):
            try:
                raw_snapshot = self.market_snapshot(ticker)
            except Exception as exc:  # noqa: BLE001 - isolate one bad ticker
                _log.warning(
                    "signal_streams.insider.market_snapshot_failed",
                    ticker=ticker,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
                continue
            if isinstance(raw_snapshot, InsiderMarketSnapshot):
                snapshot = raw_snapshot
            elif isinstance(raw_snapshot, Mapping):
                snapshot = InsiderMarketSnapshot(
                    price=raw_snapshot.get("price"),
                    market_cap=raw_snapshot.get("market_cap"),
                    average_volume=raw_snapshot.get("average_volume"),
                    quote_source_url=str(
                        raw_snapshot.get("quote_source_url") or ""
                    ),
                )
            else:
                snapshot = InsiderMarketSnapshot(
                    price=getattr(raw_snapshot, "price", None),
                    market_cap=getattr(raw_snapshot, "market_cap", None),
                    average_volume=getattr(raw_snapshot, "average_volume", None),
                    quote_source_url=str(
                        getattr(raw_snapshot, "quote_source_url", "") or ""
                    ),
                )
            snapshots[ticker] = snapshot
            if _positive_number(snapshot.price) is None:
                _log.warning(
                    "signal_streams.insider.market_snapshot_incomplete",
                    ticker=ticker,
                    missing="price",
                )
                continue
            clear_mask = _PENDING_WARNING
            if _positive_number(snapshot.market_cap) is not None:
                clear_mask |= _PENDING_BUY
            else:
                _log.warning(
                    "signal_streams.insider.market_snapshot_incomplete",
                    ticker=ticker,
                    missing="market_cap",
                )
            clear_masks[ticker] = clear_mask
        nominations = classify_prepared_insider_windows(
            prepared,
            snapshots=snapshots,
            config=self.config,
        )
        nominations = [
            nomination
            for nomination in nominations
            if (
                buy_evaluation_groups
                if nomination.direction == "long"
                else warning_evaluation_groups
            ).intersection(
                transaction.get("filing_identity", "")
                for transaction in nomination.evidence.get("transactions", [])
            )
        ]
        for event_key, event in list(working.items()):
            clear_mask = clear_masks.get(event.ticker, 0)
            if event.active == 1 and event.evaluation_pending and clear_mask:
                working[event_key] = replace(
                    event,
                    evaluation_pending=(
                        event.evaluation_pending
                        & (_PENDING_ALL ^ clear_mask)
                    ),
                )

        if session is not None:
            observed_at = self.observed_at()
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            else:
                observed_at = observed_at.astimezone(UTC)
            for event_key, event in working.items():
                row = existing_by_key.get(event_key)
                if row is None:
                    session.add(
                        SignalStreamEvent(
                            user_id=self.user_id,
                            stream=self.name,
                            event_key=event.event_key,
                            event_group_key=event.event_group_key,
                            ticker=event.ticker,
                            event_at=event.event_at,
                            available_at=event.available_at,
                            payload_json=event.payload_json,
                            source_urls_json=event.source_urls_json,
                            active=event.active,
                            evaluation_pending=event.evaluation_pending,
                            first_seen_at=observed_at,
                            last_seen_at=observed_at,
                        )
                    )
                    continue
                desired = (
                    event.event_group_key,
                    event.ticker,
                    event.event_at,
                    event.available_at,
                    event.payload_json,
                    event.source_urls_json,
                    event.active,
                    event.evaluation_pending,
                )
                current = (
                    row.event_group_key,
                    row.ticker,
                    row.event_at,
                    row.available_at,
                    row.payload_json,
                    row.source_urls_json,
                    row.active,
                    row.evaluation_pending,
                )
                if desired != current:
                    (
                        row.event_group_key,
                        row.ticker,
                        row.event_at,
                        row.available_at,
                        row.payload_json,
                        row.source_urls_json,
                        row.active,
                        row.evaluation_pending,
                    ) = desired
                if event_key in incoming_keys:
                    row.last_seen_at = observed_at
        return nominations


__all__ = [
    "InsiderClusterConfig",
    "InsiderClusterStream",
    "InsiderMarketSnapshot",
    "InsiderWindowCandidate",
    "PreparedInsiderWindows",
    "YFinanceInsiderMarketProvider",
    "classify_prepared_insider_windows",
    "classify_insider_transactions",
    "cluster_strength",
    "latest_completed_sec_day",
    "market_cap_floor",
    "potential_insider_nomination_tickers",
    "prepare_insider_windows",
]
