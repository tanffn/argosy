"""Government-contract early-signal stream backed by USAspending."""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.services.signal_streams.base import SignalNomination
from argosy.state.models import RecipientResolution

USASPENDING_ENDPOINT = (
    "https://api.usaspending.gov/api/v2/search/spending_by_award/"
)
AWARD_TYPE_CODES: tuple[str, ...] = ("A", "B", "C", "D")
_log = get_logger("argosy.services.signal_streams.contracts")

_PUBLIC_CONTRACTORS: dict[str, str] = {
    "PLTR": "Palantir Technologies Inc",
    "LMT": "Lockheed Martin Corporation",
    "NOC": "Northrop Grumman Corporation",
    "RTX": "RTX Corporation",
    "GD": "General Dynamics Corporation",
    "BA": "The Boeing Company",
    "LDOS": "Leidos Holdings Inc",
    "SAIC": "Science Applications International Corporation",
    "KTOS": "Kratos Defense and Security Solutions Inc",
    "BWXT": "BWX Technologies Inc",
    "AVAV": "AeroVironment Inc",
    "PSN": "Parsons Corporation",
    "CACI": "CACI International Inc",
    "BAH": "Booz Allen Hamilton Holding Corporation",
    "MRCY": "Mercury Systems Inc",
}

_CURATED_ALIASES: dict[str, str] = {
    "palantir technologies": "PLTR",
    "palantir technologies inc": "PLTR",
    "lockheed martin": "LMT",
    "lockheed martin corporation": "LMT",
    "northrop grumman": "NOC",
    "northrop grumman systems corporation": "NOC",
    "raytheon": "RTX",
    "raytheon company": "RTX",
    "rtx corporation": "RTX",
    "general dynamics": "GD",
    "general dynamics corporation": "GD",
    "the boeing company": "BA",
    "boeing company": "BA",
    "leidos": "LDOS",
    "leidos inc": "LDOS",
    "science applications international corporation": "SAIC",
    "saic": "SAIC",
}


@dataclass(frozen=True)
class GovContractsConfig:
    materiality_threshold: float = 0.05
    lookback_days: int = 90
    recent_scan_days: int = 2
    max_pages_per_query: int = 10

    def __post_init__(self) -> None:
        if not 0 < self.materiality_threshold <= 1:
            raise ValueError("materiality_threshold must be in (0, 1]")
        if self.lookback_days <= 0:
            raise ValueError("lookback_days must be positive")
        if not 0 < self.recent_scan_days <= self.lookback_days:
            raise ValueError(
                "recent_scan_days must be positive and no greater than lookback_days"
            )
        if self.max_pages_per_query <= 0:
            raise ValueError("max_pages_per_query must be positive")


@dataclass(frozen=True)
class MarketSnapshot:
    price: float | None
    market_cap: float | None
    average_volume: float | None
    trailing_12m_revenue: float | None
    revenue_source_url: str
    quote_source_url: str


@dataclass(frozen=True)
class ContractAward:
    award_id: str
    recipient_name: str
    obligated_amount: float
    event_date: date
    stable_id: str
    award_url: str
    raw: dict[str, Any]


def _normalise_name(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", value)


def build_usaspending_payload(
    *,
    start: date,
    end: date,
    recipient_search_text: str | None = None,
) -> dict[str, Any]:
    """Build the prime-contract award search request."""
    filters: dict[str, Any] = {
        "time_period": [
            {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            }
        ],
        "award_type_codes": list(AWARD_TYPE_CODES),
    }
    if recipient_search_text:
        filters["recipient_search_text"] = [recipient_search_text]
    return {
        "subawards": False,
        "filters": filters,
        "fields": [
            "Award ID",
            "Recipient Name",
            "Award Amount",
            "Base Obligation Date",
            "generated_internal_id",
        ],
        "page": 1,
        "limit": 100,
        "sort": "Base Obligation Date",
        "order": "desc",
    }


def parse_usaspending_awards(payload: Any) -> list[ContractAward]:
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    awards: list[ContractAward] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        award_id = str(row.get("Award ID") or "").strip()
        recipient = str(row.get("Recipient Name") or "").strip()
        event_raw = row.get("Base Obligation Date")
        amount_raw = row.get("Award Amount")
        stable_id = str(
            row.get("generated_internal_id") or award_id
        ).strip()
        if not award_id or not recipient or not event_raw or not stable_id:
            continue
        try:
            event_date = date.fromisoformat(str(event_raw)[:10])
            obligated = float(amount_raw)
        except (TypeError, ValueError):
            continue
        if obligated <= 0:
            continue
        safe_raw = {
            key: value
            for key, value in row.items()
            if "potential" not in key.lower() and "ceiling" not in key.lower()
        }
        awards.append(
            ContractAward(
                award_id=award_id,
                recipient_name=recipient,
                obligated_amount=obligated,
                event_date=event_date,
                stable_id=stable_id,
                award_url=(
                    "https://www.usaspending.gov/award/"
                    f"{quote(stable_id, safe='')}/"
                ),
                raw=safe_raw,
            )
        )
    return awards


def materiality_ratio(
    trailing_obligated_amount: float, trailing_12m_revenue: float
) -> float:
    if trailing_12m_revenue <= 0:
        return 0.0
    return trailing_obligated_amount / trailing_12m_revenue


def strength_from_materiality(ratio: float, *, threshold: float) -> float:
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    return max(0.0, min(1.0, ratio / threshold))


LlmChoice = Callable[[str, dict[str, str]], str | None]


class _RecipientResolutionOutput(BaseModel):
    ticker: str | None = None
    rationale: str = ""


def _default_llm_choice(
    recipient: str, candidates: dict[str, str]
) -> str | None:
    """Ask an LLM to choose only among pre-filtered plausible candidates."""
    from argosy.agents.base import BaseAgent

    class _ResolverAgent(BaseAgent[_RecipientResolutionOutput]):
        agent_role = "signal_recipient_resolver"
        output_model = _RecipientResolutionOutput
        require_citations = False

        def build_prompt(self, **kwargs):
            choices = "\n".join(
                f"- {ticker}: {name}"
                for ticker, name in kwargs["candidates"].items()
            )
            return (
                "Resolve a federal-award recipient to a listed company. "
                "Choose only a ticker in the supplied candidate list, or null. "
                "Never infer a public ticker for a private/unknown recipient.",
                f"RECIPIENT: {kwargs['recipient']}\nCANDIDATES:\n{choices}",
            )

    report = _ResolverAgent(user_id="system").run_sync(
        recipient=recipient, candidates=candidates
    )
    chosen = report.output.ticker
    return chosen if chosen in candidates else None


class RecipientResolver:
    """Persist-once recipient resolver with safe unresolved tombstones."""

    def __init__(
        self,
        *,
        public_companies: dict[str, str] | None = None,
        llm_choice: LlmChoice | None = None,
        fuzzy_cutoff: float = 0.72,
        automatic_match_cutoff: float = 0.92,
    ) -> None:
        self.public_companies = public_companies or dict(_PUBLIC_CONTRACTORS)
        self.llm_choice = llm_choice or _default_llm_choice
        self.fuzzy_cutoff = fuzzy_cutoff
        self.automatic_match_cutoff = automatic_match_cutoff

    def resolve(self, session: Session, recipient_name: str) -> str | None:
        normalised = _normalise_name(recipient_name)
        pending_key = "signal_recipient_resolutions_pending"
        pending: dict[str, RecipientResolution] = session.info.setdefault(
            pending_key, {}
        )
        cached = pending.get(normalised)
        if cached is not None:
            if cached in session:
                return cached.ticker
            # Rollback/expunge made the cached object transient. Drop this
            # session-local entry and re-resolve from durable state.
            pending.pop(normalised, None)

        # A pending resolution for another recipient must not autoflush here:
        # fetch still has independent cache-backed adapters to call.
        with session.no_autoflush:
            existing = session.get(RecipientResolution, normalised)
        if existing is not None:
            return existing.ticker

        ticker = _CURATED_ALIASES.get(normalised)
        method = "seed" if ticker else "unresolved"
        candidates: dict[str, str] = {}
        if ticker is None:
            scored = sorted(
                (
                    SequenceMatcher(
                        None, normalised, _normalise_name(company_name)
                    ).ratio(),
                    symbol,
                    company_name,
                )
                for symbol, company_name in self.public_companies.items()
            )
            plausible = [
                item for item in scored if item[0] >= self.fuzzy_cutoff
            ][-5:]
            candidates = {
                symbol: company_name
                for _, symbol, company_name in reversed(plausible)
            }
            if plausible:
                best_score, best_symbol, _ = plausible[-1]
                second_score = plausible[-2][0] if len(plausible) > 1 else 0.0
                if (
                    best_score >= self.automatic_match_cutoff
                    and best_score - second_score >= 0.08
                ):
                    ticker = best_symbol
                    method = "fuzzy"
                else:
                    try:
                        chosen = self.llm_choice(recipient_name, candidates)
                        if chosen in candidates:
                            ticker = chosen
                            method = "llm"
                    except Exception as exc:  # noqa: BLE001 - fail closed per recipient
                        method = "agent_error"
                        _log.warning(
                            "signal_streams.recipient_resolver.agent_failed",
                            recipient=recipient_name,
                            candidates=sorted(candidates),
                            error_type=type(exc).__name__,
                            error=str(exc)[:300],
                        )

        row = RecipientResolution(
            recipient_normalized=normalised,
            recipient_name=recipient_name,
            ticker=ticker,
            resolution_method=method,
            candidates_json=json.dumps(
                sorted(candidates), separators=(",", ":")
            ),
        )
        session.add(row)
        pending[normalised] = row
        return ticker


def _post_usaspending(payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        USASPENDING_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Argosy signal-streams/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


class ArgosyMarketSnapshotProvider:
    """Compose Argosy's cached fundamentals and market-data adapters."""

    def __init__(
        self,
        *,
        fundamentals_gatherer: Callable[..., dict[str, dict[str, Any]]] | None = None,
        market_adapter: Any | None = None,
    ) -> None:
        self._fundamentals_gatherer = fundamentals_gatherer
        self._market_adapter = market_adapter

    def __call__(self, ticker: str) -> MarketSnapshot:
        gatherer = self._fundamentals_gatherer
        if gatherer is None:
            from argosy.orchestrator.flows.plan_synthesis.inputs import (
                _gather_fundamentals,
            )

            gatherer = _gather_fundamentals
        adapter = self._market_adapter
        if adapter is None:
            from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

            adapter = YFinanceAdapter()
        fundamentals = gatherer(
            [ticker], with_yfinance_fallback=True
        ).get(ticker, {})
        market = asyncio.run(adapter.get_quote_with_fundamentals(ticker))
        revenue = fundamentals.get("revenue_ttm")
        return MarketSnapshot(
            price=(
                float(market["price"])
                if market.get("price") is not None
                else None
            ),
            market_cap=(
                float(market["market_cap"])
                if market.get("market_cap") is not None
                else None
            ),
            average_volume=(
                float(market["average_volume"])
                if market.get("average_volume") is not None
                else None
            ),
            trailing_12m_revenue=(
                float(revenue) if revenue is not None else None
            ),
            revenue_source_url=str(fundamentals.get("source_url") or ""),
            quote_source_url=f"https://finance.yahoo.com/quote/{ticker}",
        )


class GovContractsStream:
    """Nominate public recipients whose recent obligations are material."""

    name = "gov_contracts"

    def __init__(
        self,
        *,
        config: GovContractsConfig | None = None,
        fetch_json: Callable[[dict[str, Any]], dict[str, Any]] = _post_usaspending,
        resolver: RecipientResolver | None = None,
        market_snapshot: Callable[[str], MarketSnapshot] | None = None,
        today: Callable[[], date] = date.today,
        curated_contractors: dict[str, str] | None = None,
        max_page_attempts: int = 3,
        page_retry_backoff: tuple[float, ...] = (0.25, 0.75),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_page_attempts <= 0:
            raise ValueError("max_page_attempts must be positive")
        if any(delay < 0 for delay in page_retry_backoff):
            raise ValueError("page_retry_backoff delays must be non-negative")
        self.config = config or GovContractsConfig()
        self.fetch_json = fetch_json
        self.resolver = resolver or RecipientResolver()
        self.market_snapshot = market_snapshot or ArgosyMarketSnapshotProvider()
        self.today = today
        self.curated_contractors = (
            dict(_PUBLIC_CONTRACTORS)
            if curated_contractors is None
            else dict(curated_contractors)
        )
        self.max_page_attempts = max_page_attempts
        self.page_retry_backoff = page_retry_backoff
        self.sleep = sleep

    def _fetch_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Retry only transient transport failures for one page."""
        for attempt in range(self.max_page_attempts):
            try:
                return self.fetch_json(payload)
            except HTTPError:
                raise
            except (TimeoutError, URLError, ConnectionError):
                if attempt + 1 >= self.max_page_attempts:
                    raise
                if self.page_retry_backoff:
                    delay = self.page_retry_backoff[
                        min(attempt, len(self.page_retry_backoff) - 1)
                    ]
                    self.sleep(delay)
        raise RuntimeError("unreachable USAspending page retry state")

    def _fetch_query(
        self,
        *,
        start: date,
        end: date,
        recipient_search_text: str | None = None,
    ) -> dict[str, ContractAward]:
        """Fetch one bounded query completely or fail without partial data."""
        payload = build_usaspending_payload(
            start=start,
            end=end,
            recipient_search_text=recipient_search_text,
        )
        awards: dict[str, ContractAward] = {}
        for page in range(1, self.config.max_pages_per_query + 1):
            response = self._fetch_page({**payload, "page": page})
            for award in parse_usaspending_awards(response):
                awards[award.stable_id] = award
            metadata = (
                response.get("page_metadata", {})
                if isinstance(response, dict)
                else {}
            )
            if not (
                metadata.get("hasNext")
                or metadata.get("has_next_page")
            ):
                return awards
        query_name = recipient_search_text or "global"
        raise RuntimeError(
            "USAspending page cap exhausted before query completed: "
            f"query={query_name!r}, max_pages={self.config.max_pages_per_query}"
        )

    def fetch(
        self, session: Session, *, since: date
    ) -> list[SignalNomination]:
        through = self.today()
        window_start = through - timedelta(
            days=self.config.lookback_days - 1
        )
        recent_start = through - timedelta(
            days=self.config.recent_scan_days - 1
        )
        recent_global = self._fetch_query(
            start=recent_start,
            end=through,
        )
        awards_by_id = dict(recent_global)
        for recipient_name in self.curated_contractors.values():
            awards_by_id.update(
                self._fetch_query(
                    start=window_start,
                    end=through,
                    recipient_search_text=recipient_name,
                )
            )

        recent_resolutions: dict[str, str] = {}
        for award in recent_global.values():
            ticker = self.resolver.resolve(session, award.recipient_name)
            if ticker:
                recent_resolutions[award.stable_id] = ticker.upper()

        covered_tickers = {
            ticker.upper() for ticker in self.curated_contractors
        }
        queried_discoveries: set[str] = set()
        for award in recent_global.values():
            ticker = recent_resolutions.get(award.stable_id)
            if (
                ticker is None
                or ticker in covered_tickers
                or ticker in queried_discoveries
            ):
                continue
            queried_discoveries.add(ticker)
            awards_by_id.update(
                self._fetch_query(
                    start=window_start,
                    end=through,
                    recipient_search_text=award.recipient_name,
                )
            )

        resolved: list[tuple[ContractAward, str]] = []
        for award in awards_by_id.values():
            ticker = self.resolver.resolve(session, award.recipient_name)
            if ticker:
                resolved.append((award, ticker.upper()))

        totals: dict[str, float] = {}
        for award, ticker in resolved:
            if window_start <= award.event_date <= through:
                totals[ticker] = totals.get(ticker, 0.0) + award.obligated_amount

        snapshots: dict[str, MarketSnapshot] = {}
        nominations: list[SignalNomination] = []
        for award, ticker in resolved:
            if not since <= award.event_date <= through:
                continue
            snapshot = snapshots.setdefault(
                ticker, self.market_snapshot(ticker)
            )
            revenue = snapshot.trailing_12m_revenue
            if (
                snapshot.price is None
                or snapshot.price <= 0
                or revenue is None
                or revenue <= 0
            ):
                continue
            ratio = materiality_ratio(totals.get(ticker, 0.0), revenue)
            if ratio < self.config.materiality_threshold:
                continue
            nominations.append(
                SignalNomination(
                    ticker=ticker,
                    stream=self.name,
                    direction="long",
                    strength=strength_from_materiality(
                        ratio,
                        threshold=self.config.materiality_threshold,
                    ),
                    as_of=award.event_date,
                    dedup_key=f"usaspending:{award.stable_id}",
                    evidence={
                        "award_id": award.award_id,
                        "award_url": award.award_url,
                        "recipient_name": award.recipient_name,
                        "obligated_amount": award.obligated_amount,
                        "base_obligation_date": award.event_date.isoformat(),
                        "trailing_90d_obligated": totals[ticker],
                        "lookback_days": self.config.lookback_days,
                        "trailing_12m_revenue": revenue,
                        "materiality_ratio": ratio,
                        "revenue_source_url": snapshot.revenue_source_url,
                        "quote_source_url": snapshot.quote_source_url,
                        "price": snapshot.price,
                        "market_cap": snapshot.market_cap,
                        "average_volume": snapshot.average_volume,
                        "raw_award": award.raw,
                    },
                )
            )
        return nominations


__all__ = [
    "AWARD_TYPE_CODES",
    "ArgosyMarketSnapshotProvider",
    "ContractAward",
    "GovContractsConfig",
    "GovContractsStream",
    "MarketSnapshot",
    "RecipientResolver",
    "USASPENDING_ENDPOINT",
    "build_usaspending_payload",
    "materiality_ratio",
    "parse_usaspending_awards",
    "strength_from_materiality",
]
