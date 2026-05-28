"""Loader for the canonical sources registry.

Cached per-process via ``functools.lru_cache``; pass ``_bypass_cache=True``
in tests to force re-read. A per-user override YAML (``sources_user.yaml``,
sibling to the canonical) is merged if present; user-side keys take
precedence on collision.
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_CANONICAL = (
    Path(__file__).resolve().parents[2] / "data" / "sources.yaml"
)
_DEFAULT_USER = (
    Path(__file__).resolve().parents[2] / "data" / "sources_user.yaml"
)


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    url: str
    as_of: str
    kind: str  # "official" | "research" | "derived" | "best_effort"
    notes: str = ""


@dataclass(frozen=True)
class SourcesRegistry:
    sources: dict[str, Source]

    def get(self, source_id: str) -> Source | None:
        return self.sources.get(source_id)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=8)
def _cached_load(canonical_path: str, user_path: str) -> SourcesRegistry:
    canonical = _load_yaml(Path(canonical_path))
    user = _load_yaml(Path(user_path))
    canonical_sources = canonical.get("sources", {})
    user_sources = user.get("sources", {})
    merged: dict[str, Any] = {**canonical_sources, **user_sources}
    return SourcesRegistry(
        sources={
            sid: Source(
                id=sid,
                title=entry.get("title", ""),
                url=entry.get("url", ""),
                as_of=entry.get("as_of", ""),
                kind=entry.get("kind", "research"),
                notes=entry.get("notes", ""),
            )
            for sid, entry in merged.items()
        }
    )


def load_sources(
    *,
    canonical_path: Path | None = None,
    user_path: Path | None = None,
    _bypass_cache: bool = False,
) -> SourcesRegistry:
    cp = canonical_path or _DEFAULT_CANONICAL
    up = user_path or _DEFAULT_USER
    if _bypass_cache:
        _cached_load.cache_clear()
    return _cached_load(str(cp), str(up))
