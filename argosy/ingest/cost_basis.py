"""Stub Schwab cost-basis CSV importer.

Phase 4 work; tracked under SDD OPEN-2 (Schwab cost-basis CSV format
needs verification against the actual export). Phase 1 only sketches the
class so that Phase-1 plan-critique code can reference the type without
crashing.
"""

from __future__ import annotations

from pathlib import Path


class SchwabCostBasisImporter:
    """Placeholder for the Phase 4 lot-level cost-basis ingestion."""

    def __init__(self, csv_path: str | Path) -> None:
        self.csv_path = Path(csv_path)

    def import_lots(self) -> None:
        raise NotImplementedError(
            "Schwab CSV cost-basis import is Phase 4 work; tracked under SDD OPEN-2."
        )


__all__ = ["SchwabCostBasisImporter"]
