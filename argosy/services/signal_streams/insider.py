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
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from argosy.adapters.data.sec_form4_adapter import (
    MAX_GLOBAL_DATE_RANGE_DAYS,
    SecForm4Adapter,
)
from argosy.adapters.data.yfinance_adapter import YFinanceAdapter
from argosy.logging import get_logger
from argosy.services.signal_streams.base import SignalNomination

_log = get_logger("argosy.services.signal_streams.insider")
_C_SUITE_ACRONYM_PATTERN = re.compile(r"\b(?:ceo|cfo|coo|cto|cio|cmo|cro)\b")
_CHIEF_OFFICER_PATTERN = re.compile(r"\bchief(?:\s+[a-z]+){1,5}\s+officer\b")


@dataclass(frozen=True)
class InsiderClusterConfig:
    lookback_days: int = 14
    recent_scan_days: int = 2
    index_publication_lag_days: int = 2
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
        if self.lookback_days + self.recent_scan_days - 1 > MAX_GLOBAL_DATE_RANGE_DAYS:
            raise ValueError("lookback_days plus recent_scan_days overlap exceeds SEC range limit")
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
        if (
            self.lookback_days + self.cursor_max_catchup_days
            > MAX_GLOBAL_DATE_RANGE_DAYS
        ):
            raise ValueError(
                "lookback_days plus cursor_max_catchup_days exceeds "
                "SEC range limit"
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
    return {
        key
        for owner in _reporting_owners(row)
        if role_predicate(owner["role"])
        and (key := _owner_key(owner)) is not None
    }


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
        (
            f"{transaction['filing_identity']}:"
            f"{transaction['transaction_table']}:"
            f"{transaction['transaction_index']}"
        )
        for transaction in transactions
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


def _buy_nomination(
    ticker: str,
    rows: list[dict[str, Any]],
    *,
    snapshot: InsiderMarketSnapshot,
    config: InsiderClusterConfig,
) -> SignalNomination | None:
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
    distinct_count = len(insiders)
    if distinct_count < config.min_distinct_buyers:
        return None
    aggregate = sum(_valid_transaction_value(row) or 0.0 for row in eligible)
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
    predecessor_start = availability_since - timedelta(
        days=config.lookback_days - 1
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _deduplicate_rows(rows):
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker and _base_eligible(
            row,
            window_start=predecessor_start,
            through=through,
        ):
            grouped.setdefault(ticker, []).append(row)

    nominations: list[SignalNomination] = []
    for ticker in sorted(grouped):
        snapshot = snapshots.get(ticker)
        if snapshot is None or _positive_number(snapshot.price) is None:
            continue
        ticker_rows = sorted(grouped[ticker], key=_row_sort_key)
        candidates: dict[str, list[SignalNomination]] = {
            "long": [],
            "short": [],
        }
        window_ends = sorted(
            {
                transaction_date
                for row in ticker_rows
                if (
                    transaction_date := _parse_date(
                        row.get("transaction_date")
                    )
                )
                is not None
            }
        )
        for window_end in window_ends:
            window_start = window_end - timedelta(
                days=config.lookback_days - 1
            )
            window_rows = [
                row
                for row in ticker_rows
                if (
                    transaction_date := _parse_date(
                        row.get("transaction_date")
                    )
                )
                is not None
                and window_start <= transaction_date <= window_end
            ]
            latest_filed_at = max(
                (
                    filed_at
                    for row in window_rows
                    if (
                        filed_at := _parse_date(row.get("filed_at"))
                    )
                    is not None
                ),
                default=None,
            )
            if (
                latest_filed_at is None
                or latest_filed_at < availability_since
            ):
                continue
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
    """Fetch global Form 4 rows and classify per ticker with isolation."""

    name = "insider_cluster"

    def __init__(
        self,
        *,
        config: InsiderClusterConfig | None = None,
        sec_adapter: Any | None = None,
        market_snapshot: Callable[[str], InsiderMarketSnapshot] | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.config = config or InsiderClusterConfig()
        self.sec_adapter = sec_adapter or SecForm4Adapter()
        self.market_snapshot = market_snapshot or YFinanceInsiderMarketProvider()
        self.today = today

    def fetch(self, session: Any, *, since: date) -> list[SignalNomination]:
        del session
        through = self.today() - timedelta(
            days=self.config.index_publication_lag_days
        )
        recent_start = through - timedelta(days=self.config.recent_scan_days - 1)
        availability_since = min(since, recent_start)
        normal_start = through - timedelta(
            days=self.config.lookback_days - 1
        )
        predecessor_start = availability_since - timedelta(
            days=self.config.lookback_days - 1
        )
        requested_start = min(normal_start, predecessor_start)
        bounded_start = max(
            requested_start,
            through - timedelta(days=MAX_GLOBAL_DATE_RANGE_DAYS - 1),
        )
        rows = asyncio.run(
            self.sec_adapter.get_form4_for_date_range(
                bounded_start,
                through,
            )
        )
        tickers = sorted(
            {
                str(row.get("ticker") or "").strip().upper()
                for row in rows
                if str(row.get("ticker") or "").strip()
            }
        )
        snapshots: dict[str, InsiderMarketSnapshot] = {}
        for ticker in tickers:
            try:
                snapshots[ticker] = self.market_snapshot(ticker)
            except Exception as exc:  # noqa: BLE001 - isolate one bad ticker
                _log.warning(
                    "signal_streams.insider.market_snapshot_failed",
                    ticker=ticker,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
        return classify_insider_transactions(
            rows,
            snapshots=snapshots,
            config=self.config,
            through=through,
            availability_since=availability_since,
        )


__all__ = [
    "InsiderClusterConfig",
    "InsiderClusterStream",
    "InsiderMarketSnapshot",
    "YFinanceInsiderMarketProvider",
    "classify_insider_transactions",
    "cluster_strength",
    "market_cap_floor",
]
