"""Markdown plan parser.

Reads a plan markdown file (e.g. `Jacobs_Wealth_Plan.md`) and extracts a
lightweight `PlanDocument` with:
  - the full raw markdown
  - top-level (H1) and second-level (H2) headings, in order
  - a count of fenced code blocks ('```' / '```mermaid')
  - a list of pipe-table strings (raw blocks; not parsed into rows in
    Phase 1 — the plan-critique agent reads them as text)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field


@dataclass
class _Section:
    level: int
    title: str
    line_no: int  # 1-based


class PlanDocument(BaseModel):
    """Structured-ish view of a plan markdown document."""

    source_path: str
    raw_markdown: str
    headings: list[tuple[int, str]] = Field(
        default_factory=list,
        description="(level, title) for every heading. Level matches Markdown #-count.",
    )
    h1_titles: list[str] = Field(default_factory=list)
    h2_titles: list[str] = Field(default_factory=list)
    mermaid_blocks: int = 0
    fenced_blocks: int = 0
    table_blocks: list[str] = Field(default_factory=list)
    word_count: int = 0

    def summary(self) -> str:
        """Compact summary string for logging / UI."""
        return (
            f"PlanDocument(source={Path(self.source_path).name}, "
            f"h1={len(self.h1_titles)}, h2={len(self.h2_titles)}, "
            f"mermaid={self.mermaid_blocks}, tables={len(self.table_blocks)}, "
            f"words={self.word_count})"
        )


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def parse_plan_markdown(path: str | Path) -> PlanDocument:
    """Read a plan markdown file and produce a `PlanDocument`."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return parse_plan_markdown_text(text, source_path=str(p.resolve()))


def parse_plan_markdown_text(text: str, *, source_path: str = "<inline>") -> PlanDocument:
    headings: list[tuple[int, str]] = []
    h1: list[str] = []
    h2: list[str] = []
    mermaid_count = 0
    fenced_count = 0
    table_blocks: list[str] = []

    in_fence = False
    fence_lang = ""
    table_buf: list[str] = []

    def _flush_table() -> None:
        nonlocal table_buf
        if table_buf:
            joined = "\n".join(table_buf)
            # Heuristic: a real markdown table has both '|' and a header
            # separator line of '---'.
            if "---" in joined and "|" in joined:
                table_blocks.append(joined)
            table_buf = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                in_fence = False
                fence_lang = ""
            else:
                in_fence = True
                fenced_count += 1
                fence_lang = stripped.removeprefix("```").strip().lower()
                if fence_lang == "mermaid":
                    mermaid_count += 1
            _flush_table()
            continue
        if in_fence:
            continue

        m = _HEADING_RE.match(line)
        if m:
            _flush_table()
            level = len(m.group(1))
            title = m.group(2).strip()
            headings.append((level, title))
            if level == 1:
                h1.append(title)
            elif level == 2:
                h2.append(title)
            continue

        # Crude markdown-table detection: line contains a pipe and is not blank.
        if "|" in line:
            table_buf.append(line)
        else:
            _flush_table()

    _flush_table()

    word_count = len(re.findall(r"\b\w+\b", text))

    return PlanDocument(
        source_path=source_path,
        raw_markdown=text,
        headings=headings,
        h1_titles=h1,
        h2_titles=h2,
        mermaid_blocks=mermaid_count,
        fenced_blocks=fenced_count,
        table_blocks=table_blocks,
        word_count=word_count,
    )


__all__ = ["PlanDocument", "parse_plan_markdown", "parse_plan_markdown_text"]
