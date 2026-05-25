"""`argosy diagnose ...` — environment / adapter health probes.

Wave 3 gap-closure roadmap (W3a.A): the synthesis flow consumes outputs
from several data adapters (Finnhub, FRED, TipRanks, CapitolTrades,
SEC Form 4, SEC 13F, BoI, yfinance). When a live run produces empty
``news_payload`` / ``macro_snapshot`` / ``social_payload`` we need to
know *why*: missing API key vs. network failure vs. adapter bug.

``argosy diagnose adapters`` instantiates each adapter and runs its
cheapest available probe. Per adapter we emit one row:

  - ``ok``          — probe returned data
  - ``missing_key`` — adapter raised ``MissingAPIKeyError``
  - ``missing_data_source`` — adapter raised ``MissingDataSourceError``
                              (SDK not installed, parse failure, outage)
  - ``network_fail`` — anything else (timeout, HTTPError, etc.)

Exit code is 0 regardless of per-adapter status; the table itself is
the contract. Non-zero exits only when the probe machinery itself
errored (e.g. an import-time failure).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import typer

from argosy.adapters import MissingAPIKeyError, MissingDataSourceError
from argosy.adapters.data.boi_adapter import BoiAdapter
from argosy.adapters.data.capitoltrades_adapter import CapitolTradesAdapter
from argosy.adapters.data.finnhub_adapter import FinnhubAdapter
from argosy.adapters.data.fred_adapter import FredAdapter
from argosy.adapters.data.sec_13f_adapter import Sec13FAdapter
from argosy.adapters.data.sec_form4_adapter import SecForm4Adapter
from argosy.adapters.data.tipranks_adapter import TipRanksAdapter
from argosy.adapters.data.yfinance_adapter import YFinanceAdapter
from argosy.logging import configure_logging

app = typer.Typer(
    name="diagnose",
    help="Environment diagnostics: adapter connectivity, key resolution.",
    no_args_is_help=True,
)


# ----------------------------------------------------------------------
# Per-adapter probe registry
# ----------------------------------------------------------------------


@dataclass
class ProbeSpec:
    """One probe entry: adapter name, factory, async probe callable."""

    name: str
    factory: Callable[[], Any]
    probe: Callable[[Any], Awaitable[str]]


@dataclass
class ProbeResult:
    """Outcome of a single probe."""

    name: str
    status: str  # "ok" | "missing_key" | "missing_data_source" | "network_fail"
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"adapter": self.name, "status": self.status, "detail": self.detail}


# ----------------------------------------------------------------------
# Probe implementations — each picks the adapter's cheapest live call.
# ----------------------------------------------------------------------


async def _probe_finnhub(adapter: FinnhubAdapter) -> str:
    today = date.today()
    rows = await adapter.get_company_news(
        "AAPL", start=today - timedelta(days=2), end=today
    )
    return f"{len(rows)} headlines for AAPL"


async def _probe_fred(adapter: FredAdapter) -> str:
    today = date.today()
    rows = await adapter.get_series(
        "DGS10", start=today - timedelta(days=14), end=today
    )
    last = next(
        (r.get("value") for r in reversed(rows) if r.get("value") is not None),
        None,
    )
    return f"DGS10 last={last} ({len(rows)} obs)"


async def _probe_tipranks(adapter: TipRanksAdapter) -> str:
    payload = await adapter.get_analyst_consensus("AAPL")
    return (
        f"AAPL: {payload.get('consensus_label') or '?'} "
        f"(buy={payload.get('num_buy', 0)}, "
        f"hold={payload.get('num_hold', 0)}, "
        f"sell={payload.get('num_sell', 0)})"
    )


async def _probe_capitoltrades(adapter: CapitolTradesAdapter) -> str:
    rows = await adapter.list_recent_trades(days=7)
    return f"{len(rows)} recent trades (7d window)"


async def _probe_sec_form4(adapter: SecForm4Adapter) -> str:
    rows = await adapter.get_recent_form4_for_ticker("AAPL", days=14)
    return f"{len(rows)} recent Form 4 filings for AAPL"


async def _probe_sec_13f(adapter: Sec13FAdapter) -> str:
    rows = await adapter.list_recent_13f(days=14)
    return f"{len(rows)} recent 13F-HR filings"


async def _probe_boi(adapter: BoiAdapter) -> str:
    payload = await adapter.get_usd_nis()
    return f"USD/NIS={payload.get('rate')} (source={payload.get('source')})"


async def _probe_yfinance(adapter: YFinanceAdapter) -> str:
    quote = await adapter.get_quote("AAPL")
    return f"AAPL quote={quote.price} {quote.currency or ''}".strip()


# ----------------------------------------------------------------------
# Factories — separated so tests can monkey-patch them.
# ----------------------------------------------------------------------


def _make_finnhub() -> FinnhubAdapter:
    return FinnhubAdapter()


def _make_fred() -> FredAdapter:
    return FredAdapter()


def _make_tipranks() -> TipRanksAdapter:
    return TipRanksAdapter()


def _make_capitoltrades() -> CapitolTradesAdapter:
    return CapitolTradesAdapter()


def _make_sec_form4() -> SecForm4Adapter:
    return SecForm4Adapter()


def _make_sec_13f() -> Sec13FAdapter:
    return Sec13FAdapter()


def _make_boi() -> BoiAdapter:
    # BoI itself has no direct client wired in; the fallback chain runs
    # through FRED → yfinance. We pass adapters in so the probe actually
    # exercises one of those fallbacks rather than failing on a missing
    # ``boi_client``.
    return BoiAdapter(fred=FredAdapter(), yf=YFinanceAdapter())


def _make_yfinance() -> YFinanceAdapter:
    return YFinanceAdapter()


def _default_specs() -> list[ProbeSpec]:
    """Default adapter set probed by ``argosy diagnose adapters``."""
    return [
        ProbeSpec("finnhub", _make_finnhub, _probe_finnhub),
        ProbeSpec("fred", _make_fred, _probe_fred),
        ProbeSpec("tipranks", _make_tipranks, _probe_tipranks),
        ProbeSpec("capitoltrades", _make_capitoltrades, _probe_capitoltrades),
        ProbeSpec("sec_form4", _make_sec_form4, _probe_sec_form4),
        ProbeSpec("sec_13f", _make_sec_13f, _probe_sec_13f),
        ProbeSpec("boi", _make_boi, _probe_boi),
        ProbeSpec("yfinance", _make_yfinance, _probe_yfinance),
    ]


# ----------------------------------------------------------------------
# Probe runner
# ----------------------------------------------------------------------


def _first_line(message: str) -> str:
    """First non-empty line of ``message``, trimmed; '' if none."""
    for line in (message or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def probe_adapter(
    name: str,
    adapter: Any,
    probe: Callable[[Any], Awaitable[str]],
) -> dict[str, str]:
    """Run one async probe, classify the outcome, return a row dict.

    Each call is isolated: no exception escapes. Catches:

      - ``MissingAPIKeyError`` → ``missing_key``
      - ``MissingDataSourceError`` → ``missing_data_source``
      - ``BaseException`` (anything else, including ``KeyboardInterrupt``
        if raised inside the probe coroutine) → ``network_fail``

    Args:
        name: adapter name, copied verbatim into the result.
        adapter: adapter instance (may be a ``MagicMock`` in tests).
        probe: async callable taking the adapter, returning a detail
            string on success.

    Returns:
        ``{"adapter": name, "status": ..., "detail": ...}``.
    """
    try:
        detail = asyncio.run(probe(adapter))
        return ProbeResult(name=name, status="ok", detail=detail or "").as_dict()
    except MissingAPIKeyError as exc:
        return ProbeResult(
            name=name, status="missing_key", detail=_first_line(str(exc))
        ).as_dict()
    except MissingDataSourceError as exc:
        return ProbeResult(
            name=name, status="missing_data_source", detail=_first_line(str(exc))
        ).as_dict()
    except Exception as exc:  # pylint: disable=broad-except
        return ProbeResult(
            name=name,
            status="network_fail",
            detail=f"{type(exc).__name__}: {_first_line(str(exc))}".rstrip(": "),
        ).as_dict()


def run_probes(specs: list[ProbeSpec] | None = None) -> list[dict[str, str]]:
    """Instantiate adapters in ``specs`` and probe each.

    Adapter-instantiation failures are reported as ``network_fail`` so
    one broken factory does not abort the whole probe loop.
    """
    if specs is None:
        specs = _default_specs()
    results: list[dict[str, str]] = []
    for spec in specs:
        try:
            adapter = spec.factory()
        except MissingAPIKeyError as exc:
            results.append(
                ProbeResult(
                    name=spec.name,
                    status="missing_key",
                    detail=_first_line(str(exc)),
                ).as_dict()
            )
            continue
        except MissingDataSourceError as exc:
            results.append(
                ProbeResult(
                    name=spec.name,
                    status="missing_data_source",
                    detail=_first_line(str(exc)),
                ).as_dict()
            )
            continue
        except Exception as exc:  # pylint: disable=broad-except
            results.append(
                ProbeResult(
                    name=spec.name,
                    status="network_fail",
                    detail=f"{type(exc).__name__}: {_first_line(str(exc))}".rstrip(": "),
                ).as_dict()
            )
            continue
        results.append(probe_adapter(spec.name, adapter, spec.probe))
    return results


# ----------------------------------------------------------------------
# Table formatter
# ----------------------------------------------------------------------


def format_table(rows: list[dict[str, str]]) -> str:
    """Format probe results as a fixed-width plain-text table."""
    columns = ("adapter", "status", "detail")
    widths = {c: max(len(c), max((len(str(r.get(c) or "")) for r in rows), default=0))
              for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    sep = "  ".join("-" * widths[c] for c in columns)
    body = [
        "  ".join(str(r.get(c) or "").ljust(widths[c]) for c in columns)
        for r in rows
    ]
    return "\n".join([header, sep, *body])


# ----------------------------------------------------------------------
# CLI command
# ----------------------------------------------------------------------


@app.command("adapters")
def adapters_cmd(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Ping each data adapter and report OK / missing_key / network_fail.

    Probes are live network calls. Run order is deterministic. One slow
    adapter does not delay the others' results from being collected, but
    they ARE probed sequentially to stay polite with SEC / capitoltrades
    rate limits.
    """
    configure_logging()
    rows = run_probes()
    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    typer.echo(format_table(rows))


__all__ = [
    "ProbeResult",
    "ProbeSpec",
    "adapters_cmd",
    "app",
    "format_table",
    "probe_adapter",
    "run_probes",
]
