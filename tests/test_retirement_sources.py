"""Tests for the sources registry loader (Wave 0 · gap-foundation)."""
import pytest

from argosy.services.retirement.sources import (
    Source,
    SourcesRegistry,
    load_sources,
)


class TestLoadSources:
    def test_loads_canonical_yaml(self):
        reg = load_sources()
        assert isinstance(reg, SourcesRegistry)
        # The canonical YAML is hand-seeded with at least these:
        assert "bituach_leumi_old_age_2026" in reg.sources
        assert "bengen_1994" in reg.sources
        assert "israeli_tax_authority_pension_exemption_2025" in reg.sources

    def test_source_shape(self):
        reg = load_sources()
        bl = reg.sources["bituach_leumi_old_age_2026"]
        assert isinstance(bl, Source)
        assert bl.title.startswith("Bituach Leumi")
        assert bl.kind in ("official", "research", "derived", "best_effort")
        assert bl.as_of  # non-empty

    def test_get_returns_source(self):
        reg = load_sources()
        s = reg.get("bengen_1994")
        assert s is not None
        assert s.kind == "research"

    def test_get_returns_none_for_missing(self):
        reg = load_sources()
        assert reg.get("nonexistent_source_xyz") is None

    def test_load_sources_returns_consistent_registry(self):
        # Cached: same call should return equal data
        reg1 = load_sources()
        reg2 = load_sources()
        assert reg1.sources.keys() == reg2.sources.keys()

    def test_user_override_yaml_merged_when_present(self, tmp_path):
        canonical = tmp_path / "sources.yaml"
        canonical.write_text(
            """
sources:
  test_canonical:
    title: "Test canonical source"
    url: "https://example.com/canonical"
    as_of: "2026-01"
    kind: "research"
""",
            encoding="utf-8",
        )
        user = tmp_path / "sources_user.yaml"
        user.write_text(
            """
sources:
  test_user_only:
    title: "User-supplied source"
    url: "https://example.com/user"
    as_of: "2026-05"
    kind: "official"
  test_canonical:
    title: "User OVERRIDES canonical title"
    url: "https://example.com/canonical"
    as_of: "2026-05"
    kind: "research"
""",
            encoding="utf-8",
        )
        reg = load_sources(
            canonical_path=canonical, user_path=user, _bypass_cache=True,
        )
        assert "test_canonical" in reg.sources
        assert reg.sources["test_canonical"].title.startswith("User OVERRIDES")
        assert "test_user_only" in reg.sources

    def test_missing_user_yaml_is_not_an_error(self, tmp_path):
        canonical = tmp_path / "sources.yaml"
        canonical.write_text(
            """
sources:
  only_canonical:
    title: "Only canonical"
    url: "https://example.com"
    as_of: "2026-01"
    kind: "research"
""",
            encoding="utf-8",
        )
        nonexistent_user = tmp_path / "sources_user.yaml"
        # File does not exist; loader should handle gracefully.
        reg = load_sources(
            canonical_path=canonical,
            user_path=nonexistent_user,
            _bypass_cache=True,
        )
        assert "only_canonical" in reg.sources
