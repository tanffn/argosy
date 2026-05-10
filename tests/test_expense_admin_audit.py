"""Smoke test for `argosy expenses audit-corpus`.

The audit command is purely deterministic — it runs every parser + oracle
under a directory and reports counts. We exercise it against the curated
corpus fixtures and assert the output mentions every source we know is
present, plus the totals footer.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from argosy.cli.expenses_admin import app as expenses_app

CORPUS = Path(__file__).parent / "fixtures" / "expenses" / "corpus"


def test_audit_corpus_runs_against_fixture_corpus():
    runner = CliRunner()
    result = runner.invoke(expenses_app, [
        "audit-corpus", "--user-id", "ariel", "--dir", str(CORPUS),
    ])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert out.strip(), "audit-corpus produced empty output"

    # Header
    assert "Found" in out
    assert "files (.xls/.xlsx)" in out

    # Per-source totals must mention every parser present in the corpus.
    # Labels look like 'isracard 0235', 'discount 2923', 'max 6225',
    # 'leumi_osh' (Leumi corpus folder is named 'leumi' — no last-4 digits,
    # so the parser name alone is the label).
    assert "isracard" in out
    assert "discount" in out
    assert "max" in out
    assert "leumi_osh" in out

    # Footer table
    assert "Source" in out
    assert "Files" in out
    assert "Files OK" in out
    assert "Rows oracle" in out
    assert "Rows parsed" in out
    assert "TOTAL" in out

    # Summary line
    assert "Summary:" in out
    assert "files passed" in out


def test_audit_corpus_handles_unrecognized_file(tmp_path):
    """Unrecognized files appear with a '?' marker and don't crash the run."""
    bad = tmp_path / "garbage.xlsx"
    bad.write_bytes(b"\x00not really xlsx")
    runner = CliRunner()
    result = runner.invoke(expenses_app, [
        "audit-corpus", "--user-id", "ariel", "--dir", str(tmp_path),
    ])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # Line marker is the '?' for unrecognized; the bucket label is
    # 'unrecognized' and is surfaced in the footer.
    assert "garbage.xlsx" in out
    # Either the per-line '?' marker OR the 'unrecognized' bucket — both work.
    assert "unrecognized" in out or "?" in out


def test_audit_corpus_empty_dir_is_a_no_op(tmp_path):
    runner = CliRunner()
    result = runner.invoke(expenses_app, [
        "audit-corpus", "--user-id", "ariel", "--dir", str(tmp_path),
    ])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "Found 0 files" in out
    assert "TOTAL" in out
