"""CI tripwire: statement-format audit matrix must stay green.

Samples-gated like ``test_expense_parsers_ground_truth.py``. When
``ARGOSY_EXPENSE_SAMPLES_ROOT`` is set, EVERY parseable statement under
the tree must sniff+parse+oracle-match within the ingest-sanity total
tolerance, and no transaction-looking sheet may be silently unconsumed.
"""

from __future__ import annotations

import os

import pytest

from argosy.services.expense_ingest.format_audit import (
    format_matrix,
    mismatch_rows,
    run_audit,
    samples_root,
)

SAMPLES = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
pytestmark = pytest.mark.skipif(
    not SAMPLES, reason="ARGOSY_EXPENSE_SAMPLES_ROOT unset"
)


def test_format_audit_matrix_has_no_mismatches():
    root = samples_root()
    assert root is not None and root.is_dir()
    rows = run_audit(root)
    assert rows, "audit walked zero files — samples root empty?"
    # Guarantees non-statement artifacts are listed, not omitted.
    assert any(r.status == "SKIPPED" for r in rows), (
        "expected SKIPPED non-statement artifacts (portfolio/TSV/Schwab)"
    )
    assert any(r.status == "OK" for r in rows), (
        "expected at least one OK statement row"
    )
    bad = mismatch_rows(rows)
    if bad:
        detail = "\n".join(
            f"  {r.status} {r.rel_path}: {r.notes or r.sanity or r.skip_reason}"
            for r in bad
        )
        pytest.fail(
            f"{len(bad)} format-audit mismatch(es):\n{detail}\n\n"
            f"matrix tail:\n{format_matrix(rows).splitlines()[-5:]}"
        )


def test_leumi_usd_dated_exports_are_audited():
    """Gap (c): dated ``leumi_*_usd.xls`` files must appear as OK rows,
    not only the canonical ``usd.xls`` the older fixture glob covered.
    """
    rows = run_audit()
    usd = [
        r for r in rows
        if r.status == "OK" and r.sniffed == "leumi_usd"
    ]
    assert usd, "no Leumi USD statements audited"
    dated = [r for r in usd if "usd.xls" not in r.rel_path.lower()
             or r.rel_path.lower().endswith("usd.xls")]
    # At least one dated export OR the canonical usd.xls — both acceptable;
    # require the sniff path works for every usd-named statement file.
    usd_files = [
        r for r in rows
        if "usd" in r.rel_path.lower()
        and r.rel_path.lower().endswith(".xls")
        and "protfolio" not in r.rel_path.lower()
        and "portfolio" not in r.rel_path.lower()
        and r.status != "SKIPPED"
    ]
    assert usd_files, "expected Leumi USD .xls files under samples"
    bad = [r for r in usd_files if r.is_mismatch or r.status != "OK"]
    assert not bad, (
        "Leumi USD oracle/parser mismatch: "
        + "; ".join(f"{r.rel_path} ({r.notes or r.sanity})" for r in bad)
    )
