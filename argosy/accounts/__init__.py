"""Argosy accounts package (Phase 5).

Phase 5 introduces the `ArgonautAccount` abstraction over the IBKR
limited-account configuration in `agent_settings.limited_account`. The
account exposes value, open positions, autonomy state, and a daily
snapshot persistence helper.
"""

from argosy.accounts.argonaut import ArgonautAccount

__all__ = ["ArgonautAccount"]
