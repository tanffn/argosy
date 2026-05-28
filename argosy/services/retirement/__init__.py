"""Argosy retirement-companion engine.

Modular: each gap from the 2026-05-28 SDD review lives in its own submodule
under this package. The umbrella ``argosy/api/routes/retirement.py`` exposes
HTTP endpoints; per-feature UI lives under ``ui/src/components/retirement/``.

Cross-cutting primitives — citations / sources / reference — live at the
package root and are imported by all feature modules.

Plan reference: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``.
"""

from argosy.services.retirement.citations import (
    DERIVED,
    ValueWithRationale,
    as_dict,
)

__all__ = [
    "DERIVED",
    "ValueWithRationale",
    "as_dict",
]
