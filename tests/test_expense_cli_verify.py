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


def test_backfill_dry_run_prints_summary(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from argosy.cli.expenses_admin import app as expenses_app
    src = tmp_path / "samples" / "2026" / "6225"
    src.mkdir(parents=True)
    (src / "Apr.xlsx").write_bytes(
        (FIXTURES / "max_minimal.xlsx").read_bytes()
    )
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(expenses_app, [
        "backfill", "--user-id", "ariel", "--dir",
        str(tmp_path / "samples"), "--dry-run",
    ])
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "files: 1" in out or "1 file" in out or "found 1" in out
