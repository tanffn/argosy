"""Argosy bundled template assets.

Currently hosts Jinja templates for the weekly email digest (Spec E
commit #8 — ``email_digest.html.j2`` + ``email_digest.txt.j2``).  Other
template-rendered surfaces should land their templates here too so the
Jinja ``FileSystemLoader`` in ``argosy.services.email_digest`` can find
them without each caller composing its own loader.
"""
from __future__ import annotations

from pathlib import Path

#: Directory containing bundled template files. Used by
#: ``argosy.services.email_digest`` to build the Jinja environment.
TEMPLATES_DIR: Path = Path(__file__).parent

__all__ = ["TEMPLATES_DIR"]
