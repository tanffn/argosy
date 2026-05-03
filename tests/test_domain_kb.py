"""Validate every domain_knowledge/*.md file: frontmatter + minimum content."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from argosy.config import resolve_home

KB_ROOT = resolve_home() / "domain_knowledge"

REQUIRED_FRONTMATTER_FIELDS = ("topic", "jurisdiction", "last_verified", "next_refresh_due", "sources")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _all_kb_files() -> list[Path]:
    return sorted(KB_ROOT.rglob("*.md"))


def test_kb_directory_exists() -> None:
    assert KB_ROOT.is_dir(), f"domain_knowledge/ should exist at {KB_ROOT}"


def test_kb_seed_minimum_count() -> None:
    files = _all_kb_files()
    # SDD §7.6 priority order: 10 files for the Phase 1 seed.
    assert len(files) >= 10, (
        f"Expected at least 10 KB files for Phase 1 seed, found {len(files)}: "
        + ", ".join(str(f.relative_to(KB_ROOT)) for f in files)
    )


@pytest.mark.parametrize("path", _all_kb_files(), ids=lambda p: str(p.relative_to(KB_ROOT)))
def test_each_kb_file_has_valid_frontmatter(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    assert m is not None, f"{path.relative_to(KB_ROOT)}: missing or malformed frontmatter"
    meta = yaml.safe_load(m.group(1)) or {}
    for field in REQUIRED_FRONTMATTER_FIELDS:
        assert field in meta, f"{path.relative_to(KB_ROOT)}: frontmatter missing {field!r}"

    # Date fields must parse as YYYY-MM-DD.
    for date_field in ("last_verified", "next_refresh_due"):
        v = meta[date_field]
        # PyYAML may auto-convert ISO dates → datetime.date.
        if isinstance(v, date):
            continue
        assert isinstance(v, str)
        date.fromisoformat(v)  # will raise if malformed

    # At least one Tier-1 source.
    sources = meta["sources"] or []
    assert isinstance(sources, list) and sources, (
        f"{path.relative_to(KB_ROOT)}: sources list must be non-empty"
    )
    has_tier1 = any(
        isinstance(s, dict) and int(s.get("tier", 99)) == 1 for s in sources
    )
    assert has_tier1, f"{path.relative_to(KB_ROOT)}: at least one Tier-1 source required"

    # Body has substance beyond the frontmatter.
    body = text[m.end():]
    assert len(body.strip()) > 200, (
        f"{path.relative_to(KB_ROOT)}: body looks too short ({len(body.strip())} chars)"
    )
