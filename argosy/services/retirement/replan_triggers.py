"""Replan triggers — when does the projection get re-computed (MED #25).

Trigger registry (event names that fire a recompute):
  - market_drawdown_15pct   (S&P 500 drop > 15% peak-to-trough)
  - job_change              (income source change)
  - tax_law_change          (ITA / Bituach Leumi rule update)
  - health_event            (LTC need, major illness)
  - fx_shock_10pct          (USD/NIS move > 10% in a month)
  - life_event              (birth, death, marriage, divorce, IDF service)
  - user_request            (manual re-compute on /retirement page)
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


TriggerKind = Literal[
    "market_drawdown_15pct",
    "job_change",
    "tax_law_change",
    "health_event",
    "fx_shock_10pct",
    "life_event",
    "user_request",
]


@dataclass(frozen=True)
class ReplanTrigger:
    trigger_id: str
    kind: TriggerKind
    fired_at: datetime
    cause: str  # one-sentence summary
    recompute_status: Literal["pending", "running", "complete"] = "pending"


def list_known_triggers() -> list[dict]:
    """Return the registry of known trigger kinds + their descriptions."""
    return [
        {"kind": "market_drawdown_15pct",
         "description": "S&P 500 peak-to-trough drawdown > 15%"},
        {"kind": "job_change",
         "description": "Primary or partner income source change"},
        {"kind": "tax_law_change",
         "description": "Israeli Tax Authority / Bituach Leumi rule update"},
        {"kind": "health_event",
         "description": "Major illness, LTC onset, or healthcare cost spike"},
        {"kind": "fx_shock_10pct",
         "description": "USD/NIS move > 10% in a single month"},
        {"kind": "life_event",
         "description": (
             "Life event added/edited — cashflow series shape changed; "
             "projection should re-compose. Spec D commit #3: trigger "
             "still fires on create/edit (the user's recorded state "
             "changed); the semantic shifted from 'retirement-date "
             "clamp' to 'cashflow shape modifier'."
         )},
        {"kind": "user_request",
         "description": "User-initiated recompute via /retirement page"},
    ]
