"""GET /api/config — home-page config counts derived from the live codebase.

The home page used to hardcode the agent fleet size as a literal. That number
drifts the moment an agent class is added or removed. This endpoint derives the
count at runtime from the authoritative source — the public ``class <Name>Agent``
declarations under ``argosy/agents/`` — so the UI always shows the real fleet
size without anyone re-counting by hand.

Derivation rule (mirrors the grep documented in the agents package): count every
public ``class \\w+Agent`` declaration, excluding the abstract ``BaseAgent`` base
and the private ``_ResearcherAgent`` helper. ``RiskOfficerAgent`` counts once
even though it is instantiated three times per decision (perspective kwarg).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

# Top-level `class FooAgent` declarations (no leading indent => not nested).
_AGENT_CLASS_RE = re.compile(r"^class ([A-Za-z_]\w*Agent)\b", re.MULTILINE)

# Excluded from the fleet count: the abstract base + the private researcher
# helper (its two public subclasses Bull/BearResearcherAgent are counted).
_EXCLUDED_AGENT_CLASSES = frozenset({"BaseAgent", "_ResearcherAgent"})

_AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"


@lru_cache(maxsize=1)
def derive_fleet_count() -> int:
    """Return the number of public agent classes under ``argosy/agents/``.

    Scans the package source so the count tracks the codebase rather than a
    hand-maintained literal. Cached because the source can't change within a
    running process.
    """
    names: set[str] = set()
    for path in _AGENTS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _AGENT_CLASS_RE.finditer(text):
            name = match.group(1)
            if name not in _EXCLUDED_AGENT_CLASSES:
                names.add(name)
    return len(names)


class ConfigResponse(BaseModel):
    # Number of distinct public agent classes in the fleet, derived from the
    # codebase (see derive_fleet_count). Drives the "<N> agents" hero copy.
    fleet_count: int


@router.get("/config", response_model=ConfigResponse)
def config() -> ConfigResponse:
    return ConfigResponse(fleet_count=derive_fleet_count())
