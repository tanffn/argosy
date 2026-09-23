"""Primary SEC earnings-filing coverage for held U.S.-listed stocks.

The calendar monitor tells Argosy *when* results happened.  This monitor stores
proof that EDGAR was checked and turns a recent 8-K/10-Q/10-K/6-K/20-F results
filing into the existing news-analysis path.  Full filing text is retained for
citation display; only a bounded evidence excerpt reaches an LLM.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from lxml import html
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from argosy.adapters.data.sec_13f_adapter import _default_headers
from argosy.services.news_extractor import extract
from argosy.state.models import EarningsCoverageReceipt, NewsSignal

_PROVIDER = "sec_edgar"
_NEWS_SOURCE = "sec_filing"
_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
_RESULT_FORMS = frozenset({"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A", "6-K", "6-K/A", "20-F", "20-F/A"})
_RESULT_TERMS = re.compile(
    r"(?i)\b(earnings|financial results|quarterly results|results of operations|"
    r"revenue|net income|guidance|outlook|earnings per share|diluted eps)\b"
)
_DOCUMENT_RE = re.compile(r"(?is)<DOCUMENT>(.*?)</DOCUMENT>")
_MAX_RAW_TEXT = 100_000
_MAX_EXCERPT = 280
_PERIODIC_FORMS = frozenset({"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A"})


@dataclass(frozen=True)
class SecEarningsFiling:
    ticker: str
    cik: str
    accession: str
    form: str
    filed_at: datetime
    report_date: str
    items: str
    source_url: str
    evidence_excerpt: str
    raw_text: str

    def receipt_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("raw_text", None)
        value["filed_at"] = self.filed_at.isoformat()
        return value


@dataclass(frozen=True)
class SecEarningsCoverageSummary:
    attempted: int
    succeeded: int
    empty: int
    failures: int
    recent_filings: int
    signals_persisted: int
    signals_duplicate: int
    errors: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "errors": list(self.errors)}


class SecEarningsFetcher:
    """Small synchronous EDGAR client with a per-run ticker-map cache."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        request_interval_seconds: float = 0.12,
    ) -> None:
        if request_interval_seconds < 0.11:
            raise ValueError("SEC request interval must be at least 0.11 seconds")
        self._client = client or httpx.Client(timeout=20.0, headers=_default_headers())
        self._owns_client = client is None
        self._interval = request_interval_seconds
        self._last_request_at = 0.0
        self._ticker_map: dict[str, str] | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get_json(self, url: str) -> dict[str, Any]:
        self._pace()
        response = self._client.get(url)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError(f"SEC returned non-object JSON for {url}")
        return value

    def _get_text(self, url: str) -> str:
        self._pace()
        response = self._client.get(url)
        response.raise_for_status()
        return response.text

    def _pace(self) -> None:
        wait = self._interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _cik_for(self, ticker: str) -> str:
        if self._ticker_map is None:
            payload = self._get_json(_TICKER_MAP_URL)
            self._ticker_map = {
                str(row.get("ticker") or "").upper(): str(row.get("cik_str") or "").zfill(10)
                for row in payload.values()
                if isinstance(row, dict) and row.get("ticker") and row.get("cik_str")
            }
        symbol = ticker.upper()
        aliases = (symbol, symbol.replace("/", "-"), symbol.replace(".", "-"))
        cik = next(
            (self._ticker_map.get(alias) for alias in aliases if self._ticker_map.get(alias)),
            None,
        )
        if not cik:
            raise ValueError(f"SEC ticker map has no CIK for {ticker}")
        return cik

    def __call__(
        self,
        ticker: str,
        *,
        cutoff: date,
    ) -> list[SecEarningsFiling]:
        symbol = ticker.strip().upper()
        cik = self._cik_for(symbol)
        payload = self._get_json(_SUBMISSIONS_URL.format(cik=cik))
        recent = ((payload.get("filings") or {}).get("recent") or {})
        rows = _recent_rows(recent)
        candidates = [
            row for row in rows
            if row["filed_at"] >= cutoff and _is_candidate(row["form"], row["items"])
        ]
        # Prefer an earnings 8-K/6-K over the same day's periodic report.
        candidates.sort(
            key=lambda row: (row["filed_at"], _form_priority(row["form"])),
            reverse=True,
        )
        # A reviewer needs both the latest periodic financial statements and
        # the latest results release. Keep at most one of each. This bounds
        # network traffic while avoiding the old mistake of treating "nothing
        # filed in ten days" as "no primary evidence".
        periodic = next(
            (row for row in candidates if row["form"] in _PERIODIC_FORMS),
            None,
        )
        result_rows = [
            row for row in candidates
            if row["form"].startswith(("8-K", "6-K"))
        ]
        rows_to_fetch = ([periodic] if periodic is not None else []) + result_rows[:6]
        events: list[SecEarningsFiling] = []
        have_periodic = False
        have_results = False
        seen_accessions: set[str] = set()
        for row in rows_to_fetch:
            if row["accession"] in seen_accessions:
                continue
            if row["form"] in _PERIODIC_FORMS and have_periodic:
                continue
            if row["form"].startswith(("8-K", "6-K")) and have_results:
                continue
            accession = row["accession"]
            accession_path = accession.replace("-", "")
            source_url = (
                f"{_ARCHIVES_BASE}/{int(cik)}/{accession_path}/"
                f"{accession}-index.html"
            )
            submission_url = (
                f"{_ARCHIVES_BASE}/{int(cik)}/{accession_path}/{accession}.txt"
            )
            submission = self._get_text(submission_url)
            filing_text = _select_filing_text(submission, form=row["form"])
            if not filing_text:
                continue
            # 6-K/20-F traffic is broad; require actual results language.
            if row["form"].startswith(("6-K", "20-F")) and not _RESULT_TERMS.search(filing_text):
                continue
            excerpt = _make_excerpt(
                ticker=symbol,
                form=row["form"],
                filed_at=row["filed_at"],
                items=row["items"],
                text=filing_text,
            )
            filed_at = datetime.combine(row["filed_at"], datetime.min.time(), tzinfo=UTC)
            events.append(SecEarningsFiling(
                ticker=symbol,
                cik=cik,
                accession=accession,
                form=row["form"],
                filed_at=filed_at,
                report_date=row["report_date"],
                items=row["items"],
                source_url=source_url,
                evidence_excerpt=excerpt,
                raw_text=filing_text[:_MAX_RAW_TEXT],
            ))
            seen_accessions.add(accession)
            if row["form"] in _PERIODIC_FORMS:
                have_periodic = True
            else:
                have_results = True
        return events


def _recent_rows(recent: dict[str, Any]) -> list[dict[str, Any]]:
    keys = ("form", "filingDate", "reportDate", "accessionNumber", "items")
    columns = {key: list(recent.get(key) or []) for key in keys}
    size = min((len(values) for values in columns.values()), default=0)
    out: list[dict[str, Any]] = []
    for index in range(size):
        try:
            filed_at = date.fromisoformat(str(columns["filingDate"][index]))
        except ValueError:
            continue
        out.append({
            "form": str(columns["form"][index] or "").upper(),
            "filed_at": filed_at,
            "report_date": str(columns["reportDate"][index] or ""),
            "accession": str(columns["accessionNumber"][index] or ""),
            "items": str(columns["items"][index] or ""),
        })
    return [row for row in out if row["accession"]]


def _is_candidate(form: str, items: str) -> bool:
    if form not in _RESULT_FORMS:
        return False
    if form.startswith("8-K"):
        return "2.02" in {part.strip() for part in items.split(",")}
    return True


def _form_priority(form: str) -> int:
    if form.startswith("8-K"):
        return 4
    if form.startswith("6-K"):
        return 3
    if form.startswith("10-Q"):
        return 2
    return 1


def _select_filing_text(submission: str, *, form: str) -> str:
    documents: list[tuple[int, str]] = []
    for block in _DOCUMENT_RE.findall(submission):
        doc_type_match = re.search(r"(?im)^<TYPE>\s*([^\r\n<]+)", block)
        description_match = re.search(r"(?im)^<DESCRIPTION>\s*([^\r\n<]+)", block)
        text_match = re.search(r"(?is)<TEXT>(.*?)(?:</TEXT>|$)", block)
        if text_match is None:
            continue
        doc_type = (doc_type_match.group(1).strip().upper() if doc_type_match else "")
        description = description_match.group(1).strip() if description_match else ""
        plain = _plain_text(text_match.group(1))
        if not plain:
            continue
        earnings_description = bool(_RESULT_TERMS.search(description))
        if doc_type.startswith("EX-99") and earnings_description:
            rank = 0
        elif doc_type.startswith("EX-99") and _RESULT_TERMS.search(plain):
            rank = 1
        elif doc_type == form or doc_type.rstrip("/A") == form.rstrip("/A"):
            rank = 2
        else:
            rank = 5
        documents.append((rank, plain))
    if not documents:
        return _plain_text(submission)
    documents.sort(key=lambda item: item[0])
    return documents[0][1]


def _plain_text(value: str) -> str:
    try:
        root = html.fromstring(value)
        text = root.text_content()
    except Exception:  # noqa: BLE001 - malformed legacy filing fallback
        text = re.sub(r"(?is)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", text).strip()


def _make_excerpt(
    *, ticker: str, form: str, filed_at: date, items: str, text: str,
) -> str:
    prefix = f"{ticker} filed primary SEC {form} on {filed_at.isoformat()}"
    if items:
        prefix += f" (items {items})"
    prefix += ". "
    normalized = re.sub(r"\s+", " ", text).strip()
    matches = list(_RESULT_TERMS.finditer(normalized))
    windows: list[str] = []
    for match in matches[:40]:
        start = max(0, match.start() - 70)
        end = min(len(normalized), match.end() + 360)
        windows.append(normalized[start:end].strip(" .;"))
    if windows:
        def score(window: str) -> tuple[int, int]:
            useful = sum(token in window.lower() for token in (
                "increase", "decrease", "grew", "declined", "guidance", "outlook",
            ))
            numeric = sum(char.isdigit() for char in window)
            return useful * 20 + min(numeric, 20), -len(window)
        body = max(windows, key=score)
    else:
        body = normalized[:500]
    return (prefix + body)[:_MAX_EXCERPT]


def _write_receipt(
    session: Session,
    *,
    user_id: str,
    ticker: str,
    checked_at: datetime,
    status: str,
    events: list[SecEarningsFiling],
    error: str | None,
) -> None:
    values = {
        "user_id": user_id,
        "ticker": ticker,
        "check_date": checked_at.date(),
        "checked_at": checked_at,
        "provider": _PROVIDER,
        "status": status,
        "source_url": "https://www.sec.gov/edgar/search/",
        "events_json": json.dumps(
            [event.receipt_dict() for event in events],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "latest_reported_at": max((event.filed_at for event in events), default=None),
        "next_scheduled_at": None,
        "error_message": error,
    }
    if session.get_bind().dialect.name == "sqlite":
        statement = sqlite_insert(EarningsCoverageReceipt).values(**values)
        excluded = statement.excluded
        current = EarningsCoverageReceipt.__table__.c
        # A same-day retry may diagnose an outage, but it must not destroy the
        # last-known-good filing packet. Keep this rule in the atomic UPSERT
        # so two scheduler processes cannot race good evidence to empty/error.
        may_replace = ~(
            (
                current.status.in_(("ok", "empty"))
                & (excluded.status == "error")
            )
            | (
                (current.status == "ok")
                & (current.events_json != "[]")
                & (excluded.status == "empty")
            )
        )
        session.execute(statement.on_conflict_do_update(
            index_elements=["user_id", "ticker", "provider", "check_date"],
            set_={
                "checked_at": excluded.checked_at,
                "status": excluded.status,
                "source_url": excluded.source_url,
                "events_json": excluded.events_json,
                "latest_reported_at": excluded.latest_reported_at,
                "next_scheduled_at": excluded.next_scheduled_at,
                "error_message": excluded.error_message,
            },
            where=may_replace,
        ))
        return
    row = session.execute(select(EarningsCoverageReceipt).where(
        EarningsCoverageReceipt.user_id == user_id,
        EarningsCoverageReceipt.ticker == ticker,
        EarningsCoverageReceipt.provider == _PROVIDER,
        EarningsCoverageReceipt.check_date == checked_at.date(),
    )).scalar_one_or_none()
    if row is None:
        session.add(EarningsCoverageReceipt(**values))
        return
    if (
        row.status in {"ok", "empty"} and status == "error"
    ) or (
        row.status == "ok" and row.events_json != "[]" and status == "empty"
    ):
        return
    for field, value in values.items():
        setattr(row, field, value)


def _write_signal(session: Session, event: SecEarningsFiling) -> bool:
    existing = session.execute(select(NewsSignal.id).where(
        NewsSignal.source == _NEWS_SOURCE,
        NewsSignal.source_ref == event.source_url,
    )).first()
    if existing is not None:
        return False
    normalized = extract(
        source="sec_filing",
        source_ref=event.source_url,
        raw_text=event.evidence_excerpt,
        received_at=event.filed_at,
        known_tickers=frozenset({event.ticker}),
    )
    keywords = sorted(set(normalized.event_keywords) | {"earnings", "sec_filing"})
    session.add(NewsSignal(
        source=_NEWS_SOURCE,
        source_ref=event.source_url,
        received_at=event.filed_at,
        parsed_tickers=json.dumps([event.ticker]),
        event_keywords=json.dumps(keywords),
        sentiment=normalized.sentiment,
        source_trust="high",
        evidence_excerpt=event.evidence_excerpt,
        raw_text=event.raw_text,
    ))
    session.flush()
    return True


def run_sec_earnings_checks(
    session: Session,
    *,
    user_id: str,
    tickers: Iterable[str],
    checked_at: datetime | None = None,
    fetcher: Callable[..., list[SecEarningsFiling]] | None = None,
    lookback_days: int = 10,
    evidence_history_days: int = 550,
) -> SecEarningsCoverageSummary:
    now = checked_at or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    symbols = sorted({str(ticker).strip().upper() for ticker in tickers if ticker})
    owned_fetcher = SecEarningsFetcher() if fetcher is None else None
    provider = fetcher or owned_fetcher
    recent_cutoff = now - timedelta(days=max(1, int(lookback_days)))
    evidence_cutoff = now.date() - timedelta(days=max(1, int(evidence_history_days)))
    succeeded = empty = failures = recent = persisted = duplicates = 0
    errors: list[dict[str, str]] = []
    # Download before issuing ANY writes. SQLite has one writer: keeping the
    # receipt transaction open during later network requests starves the job
    # registry, quote cache and all other writers. The caller still owns one
    # atomic persistence transaction; this service must not commit its session.
    fetched: list[tuple[str, list[SecEarningsFiling], str | None]] = []
    try:
        for ticker in symbols:
            try:
                events = provider(ticker, cutoff=evidence_cutoff)  # type: ignore[misc]
                fetched.append((ticker, events, None))
            except Exception as exc:  # noqa: BLE001 - isolate per ticker
                message = str(exc)[:300]
                fetched.append((ticker, [], message))
    finally:
        if owned_fetcher is not None:
            owned_fetcher.close()
    for ticker, events, error in fetched:
        _write_receipt(
            session, user_id=user_id, ticker=ticker, checked_at=now,
            status="error" if error is not None else ("ok" if events else "empty"),
            events=events, error=error,
        )
        if error is not None:
            failures += 1
            errors.append({"ticker": ticker, "error": error})
            continue
        succeeded += 1
        empty += not events
        for event in events:
            # Historical evidence is not a backdated "fresh" signal.
            if event.filed_at < recent_cutoff:
                continue
            recent += 1
            with session.begin_nested():
                if _write_signal(session, event):
                    persisted += 1
                else:
                    duplicates += 1
    session.flush()
    return SecEarningsCoverageSummary(
        attempted=len(symbols),
        succeeded=succeeded,
        empty=int(empty),
        failures=failures,
        recent_filings=recent,
        signals_persisted=persisted,
        signals_duplicate=duplicates,
        errors=tuple(errors),
    )


__all__ = [
    "SecEarningsCoverageSummary",
    "SecEarningsFetcher",
    "SecEarningsFiling",
    "run_sec_earnings_checks",
]
