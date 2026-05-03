"""Exercise the plan markdown parser.

Prefers the user's actual `Jacobs_Wealth_Plan.md` if present; otherwise
falls back to a small inline fixture that exercises the same code paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argosy.ingest.plan import parse_plan_markdown, parse_plan_markdown_text

USER_PLAN_PATH = Path(r"D:/Google Drive/Family/Finances/Portfolio/Jacobs_Wealth_Plan.md")


def test_plan_parser_inline_fixture() -> None:
    """Always-runnable: small inline plan that exercises every code path."""
    text = (
        "# A Plan\n\n"
        "## Section One\n\n"
        "Paragraph text.\n\n"
        "| header | other |\n"
        "|---|---|\n"
        "| a | b |\n\n"
        "```mermaid\n"
        "graph LR; A-->B\n"
        "```\n\n"
        "## Section Two\n\n"
        "More text.\n"
    )
    doc = parse_plan_markdown_text(text, source_path="<inline>")
    assert doc.h1_titles == ["A Plan"]
    assert doc.h2_titles == ["Section One", "Section Two"]
    assert doc.mermaid_blocks == 1
    assert doc.fenced_blocks == 1
    assert len(doc.table_blocks) == 1
    assert doc.word_count > 0


def test_plan_parser_real_jacobs_plan() -> None:
    if not USER_PLAN_PATH.is_file():
        pytest.skip(f"Real plan not present at {USER_PLAN_PATH!s}; skipping.")
    doc = parse_plan_markdown(USER_PLAN_PATH)
    # The real Jacobs_Wealth_Plan.md is a long structured doc.
    assert doc.word_count > 1_000
    assert len(doc.headings) > 5
    # Has at least one mermaid diagram (the journey-visualized graph).
    assert doc.mermaid_blocks >= 1
    # Has multiple tables (asset/income/etc.).
    assert len(doc.table_blocks) >= 3
    # Title H1 is plausibly the path-to-FI heading.
    assert any("financial" in h.lower() or "path" in h.lower() for h in doc.h1_titles + doc.h2_titles)
