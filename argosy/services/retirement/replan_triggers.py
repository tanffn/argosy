"""Replan triggers — when does the projection get re-computed (MED #25).

Trigger registry (event names that fire a recompute):
  - market_drawdown_15pct   (S&P 500 drop > 15% peak-to-trough)
  - job_change              (income source change)
  - tax_law_change          (ITA / Bituach Leumi rule update)
  - health_event            (LTC need, major illness)
  - fx_shock_10pct          (USD/NIS move > 10% in a month)
  - life_event              (birth, death, marriage, divorce, IDF service)
  - user_request            (manual re-compute on /retirement page)

Spec E commit #4 extension — two synthetic kinds the observer→replan
dispatcher records on monitor_flag kinds that don't map cleanly to the
seven classical triggers above:

  - observer_emergent_critical          (a critical-severity observer
                                         flag whose mapping is "this
                                         warrants a replan but isn't a
                                         classical category")
  - observer_emergent_warning_dry_run   (a warning-severity observer
                                         flag whose mapping would fire
                                         IF the severity gate allowed
                                         warnings — recorded as a
                                         dry-run for visibility)

These two are NOT consumed by the plan_synthesis flow itself (the
flow's trigger taxonomy stays the seven classical kinds); they exist
to (a) satisfy the dispatcher's CHECK enum in
``replan_dispatch_log.trigger_kind`` and (b) give the operator a
discriminator on the audit log for "the dispatcher saw something but
elected not to fire" vs "the dispatcher fired a classical replan."
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


# Spec E commit #4 — synthetic trigger kinds for the observer→replan
# dispatcher's audit log. Kept SEPARATE from ``TriggerKind`` so callers
# of the plan_synthesis flow can keep their narrower Literal union; the
# dispatcher's CHECK enum is the wider superset (see
# ``replan_dispatch_log.trigger_kind`` in migration 0056).
DispatchTriggerKind = Literal[
    "market_drawdown_15pct",
    "job_change",
    "tax_law_change",
    "health_event",
    "fx_shock_10pct",
    "life_event",
    "user_request",
    "observer_emergent_critical",
    "observer_emergent_warning_dry_run",
]


#: The full set of trigger kinds the dispatcher may write — the CHECK
#: enum in migration 0056 mirrors this tuple.
ALL_DISPATCH_TRIGGER_KINDS: tuple[str, ...] = (
    "market_drawdown_15pct",
    "job_change",
    "tax_law_change",
    "health_event",
    "fx_shock_10pct",
    "life_event",
    "user_request",
    "observer_emergent_critical",
    "observer_emergent_warning_dry_run",
)


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
