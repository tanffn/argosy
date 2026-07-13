"""Deterministic statement-format audit matrix.

Walks ARGOSY_EXPENSE_SAMPLES_ROOT and prints one TSV row per file:
sniffed parser, rows parsed vs oracle, declared vs parsed total, sanity
verdict, last4 identity source. Non-statement artifacts are SKIPPED with
an explicit reason — no silent omissions.

ZERO live LLM calls. Usage (PowerShell):

  $env:PYTHONIOENCODING='utf-8'
  $env:ARGOSY_EXPENSE_SAMPLES_ROOT='D:/Google Drive/Family/Finances/Portfolio/Resources'
  .venv/Scripts/python.exe scripts/audit_statement_formats.py

Exit 0 when every parseable statement is OK; exit 1 on any FAIL/ERROR /
parser↔oracle mismatch / unconsumed-sheet hazard.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from argosy.services.expense_ingest.format_audit import (  # noqa: E402
    format_matrix,
    mismatch_rows,
    run_audit,
    samples_root,
)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    root = samples_root()
    if root is None:
        print(
            "ARGOSY_EXPENSE_SAMPLES_ROOT unset or not a directory",
            file=sys.stderr,
        )
        return 2
    if argv:
        print(f"usage: {sys.argv[0]}  (no args; root from env)", file=sys.stderr)
        return 2

    rows = run_audit(root)
    sys.stdout.write(format_matrix(rows))
    bad = mismatch_rows(rows)
    if bad:
        print(f"# FAIL: {len(bad)} mismatch(es)", file=sys.stderr)
        for r in bad:
            print(f"#   {r.status} {r.rel_path}: {r.notes or r.sanity}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
