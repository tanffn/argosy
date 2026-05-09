"""Smoke test for the expenses-verify-file CLI subcommand."""

from pathlib import Path

from typer.testing import CliRunner

from argosy.cli.expenses_admin import app as expenses_app

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def test_verify_file_isracard_minimal_passes():
    runner = CliRunner()
    result = runner.invoke(expenses_app, [
        "verify-file", str(FIXTURES / "isracard_minimal.xlsx"),
    ])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "Format:" in out
    assert "isracard" in out
    assert "Status: PASS" in out


def test_verify_file_unknown_format_exits_nonzero(tmp_path):
    bad = tmp_path / "garbage.bin"
    bad.write_bytes(b"\x00\x01\x02\x03")
    runner = CliRunner()
    result = runner.invoke(expenses_app, ["verify-file", str(bad)])
    assert result.exit_code != 0
    out = result.stdout.lower()
    assert "unrecognized" in out or "unknown" in out
