"""Unit tests for ``argosy diagnose adapters`` (Wave 3 / W3a.A).

Tests never make real network calls — probe callables are inline async
functions or the factories are monkey-patched to return ``MagicMock``s.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from typer.testing import CliRunner

from argosy.adapters import MissingAPIKeyError, MissingDataSourceError
from argosy.cli import diagnose as diagnose_cli
from argosy.cli.diagnose import (
    ProbeSpec,
    format_table,
    probe_adapter,
    run_probes,
)
from argosy.cli.main import app as root_app

runner = CliRunner()


# ----------------------------------------------------------------------
# probe_adapter — direct unit tests
# ----------------------------------------------------------------------


def test_probe_adapter_reports_ok_for_working_probe() -> None:
    async def _ok(_a: Any) -> str:
        return "probe succeeded"

    result = probe_adapter("test", MagicMock(), _ok)
    assert result["status"] == "ok"
    assert result["adapter"] == "test"
    assert result["detail"] == "probe succeeded"


def test_probe_adapter_reports_missing_key() -> None:
    async def _missing(_a: Any) -> str:
        raise MissingAPIKeyError(
            provider="Finnhub",
            keychain_key="argosy.finnhub.api_key",
            env_var="FINNHUB_API_KEY",
        )

    result = probe_adapter("finnhub", MagicMock(), _missing)
    assert result["status"] == "missing_key"
    assert "Finnhub" in result["detail"]


def test_probe_adapter_reports_missing_data_source() -> None:
    async def _no_source(_a: Any) -> str:
        raise MissingDataSourceError("fredapi package is not installed")

    result = probe_adapter("fred", MagicMock(), _no_source)
    assert result["status"] == "missing_data_source"
    assert "fredapi" in result["detail"]


def test_probe_adapter_reports_network_fail_on_generic_exception() -> None:
    async def _boom(_a: Any) -> str:
        raise RuntimeError("connection refused")

    result = probe_adapter("test", MagicMock(), _boom)
    assert result["status"] == "network_fail"
    assert "connection refused" in result["detail"]
    assert "RuntimeError" in result["detail"]


def test_probe_adapter_network_fail_uses_first_line_of_message() -> None:
    async def _boom(_a: Any) -> str:
        raise RuntimeError("first line\nsecond line should be discarded")

    result = probe_adapter("test", MagicMock(), _boom)
    assert "first line" in result["detail"]
    assert "second line" not in result["detail"]


# ----------------------------------------------------------------------
# run_probes — probe-isolation: one failure must not abort the loop
# ----------------------------------------------------------------------


def test_run_probes_isolates_failures_across_adapters() -> None:
    """A factory that raises must not prevent later probes from running."""

    def _good_factory() -> Any:
        return MagicMock(name="good_adapter")

    def _bad_factory() -> Any:
        raise MissingAPIKeyError(
            provider="Finnhub",
            keychain_key="argosy.finnhub.api_key",
            env_var="FINNHUB_API_KEY",
        )

    async def _ok(_a: Any) -> str:
        return "ok detail"

    async def _net_fail(_a: Any) -> str:
        raise RuntimeError("simulated DNS failure")

    specs = [
        ProbeSpec("first_ok", _good_factory, _ok),
        ProbeSpec("missing", _bad_factory, _ok),
        ProbeSpec("net_fail", _good_factory, _net_fail),
        ProbeSpec("last_ok", _good_factory, _ok),
    ]
    results = run_probes(specs=specs)
    assert [r["adapter"] for r in results] == [
        "first_ok",
        "missing",
        "net_fail",
        "last_ok",
    ]
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "missing_key"
    assert results[2]["status"] == "network_fail"
    assert results[3]["status"] == "ok"


def test_run_probes_handles_factory_missing_data_source() -> None:
    def _factory() -> Any:
        raise MissingDataSourceError("yfinance package not installed")

    async def _ok(_a: Any) -> str:
        return "unused"

    results = run_probes(specs=[ProbeSpec("yfinance", _factory, _ok)])
    assert results == [
        {
            "adapter": "yfinance",
            "status": "missing_data_source",
            "detail": "yfinance package not installed",
        }
    ]


def test_run_probes_handles_factory_unexpected_exception() -> None:
    def _factory() -> Any:
        raise ValueError("boot error")

    async def _ok(_a: Any) -> str:
        return "unused"

    results = run_probes(specs=[ProbeSpec("broken", _factory, _ok)])
    assert results[0]["status"] == "network_fail"
    assert "ValueError" in results[0]["detail"]
    assert "boot error" in results[0]["detail"]


# ----------------------------------------------------------------------
# format_table — basic shape check
# ----------------------------------------------------------------------


def test_format_table_renders_header_and_rows() -> None:
    rendered = format_table([
        {"adapter": "finnhub", "status": "ok", "detail": "3 headlines"},
        {"adapter": "fred", "status": "missing_key", "detail": "FRED_API_KEY missing"},
    ])
    lines = rendered.splitlines()
    assert lines[0].startswith("adapter")
    assert "status" in lines[0]
    assert "detail" in lines[0]
    assert "finnhub" in rendered
    assert "fred" in rendered
    assert "3 headlines" in rendered


# ----------------------------------------------------------------------
# CLI smoke tests (Typer CliRunner; no live network)
# ----------------------------------------------------------------------


def _patch_specs_all_ok(monkeypatch: Any, names: list[str]) -> None:
    """Replace ``_default_specs`` with stubbed all-ok specs."""

    def _factory() -> Any:
        return MagicMock()

    async def _probe(_a: Any) -> str:
        return "stub ok"

    monkeypatch.setattr(
        diagnose_cli,
        "_default_specs",
        lambda: [ProbeSpec(name, _factory, _probe) for name in names],
    )


def test_cli_adapters_outputs_table(monkeypatch: Any) -> None:
    _patch_specs_all_ok(monkeypatch, ["finnhub", "fred", "yfinance"])
    result = runner.invoke(root_app, ["diagnose", "adapters"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "adapter" in out
    assert "status" in out
    assert "finnhub" in out
    assert "fred" in out
    assert "yfinance" in out
    assert "stub ok" in out


def test_cli_adapters_json_flag_emits_json(monkeypatch: Any) -> None:
    _patch_specs_all_ok(monkeypatch, ["finnhub", "fred"])
    result = runner.invoke(root_app, ["diagnose", "adapters", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert {row["adapter"] for row in payload} == {"finnhub", "fred"}
    assert all(row["status"] == "ok" for row in payload)


def test_cli_adapters_mixed_statuses(monkeypatch: Any) -> None:
    """Each adapter classified independently; whole command still exit 0."""

    def _factory() -> Any:
        return MagicMock()

    async def _ok(_a: Any) -> str:
        return "ok"

    async def _missing(_a: Any) -> str:
        raise MissingAPIKeyError(
            provider="FRED",
            keychain_key="argosy.fred.api_key",
            env_var="FRED_API_KEY",
        )

    async def _boom(_a: Any) -> str:
        raise RuntimeError("HTTP 403 forbidden")

    monkeypatch.setattr(
        diagnose_cli,
        "_default_specs",
        lambda: [
            ProbeSpec("finnhub", _factory, _ok),
            ProbeSpec("fred", _factory, _missing),
            ProbeSpec("tipranks", _factory, _boom),
        ],
    )
    result = runner.invoke(root_app, ["diagnose", "adapters", "--json"])
    assert result.exit_code == 0, result.output
    rows = {row["adapter"]: row for row in json.loads(result.output)}
    assert rows["finnhub"]["status"] == "ok"
    assert rows["fred"]["status"] == "missing_key"
    assert rows["tipranks"]["status"] == "network_fail"
    assert "HTTP 403" in rows["tipranks"]["detail"]
