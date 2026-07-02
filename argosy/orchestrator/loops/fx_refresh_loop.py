"""Daily FX-refresh cadence loop.

USD/NIS staleness was previously only fixed on-demand (the deploy-cash / period-
directive request path). This loop keeps the cached rate fresh for ALL consumers
(retirement MC, dashboards, the TSV) so no surface silently computes on a stale
rate. Cheap: one BoI window fetch, best-effort, failure-isolated.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.fx import refresh_if_stale

_log = get_logger("argosy.loops.fx_refresh")

_DEFAULT_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Cached sync ``sessionmaker`` bound to the configured DB (lazy so import has
    no side effects; rebuilds if the db_file changes across test reloads)."""
    global _DEFAULT_SESSION_FACTORY

    import sqlalchemy as sa

    from argosy.config import get_settings

    db_file = str(get_settings().db_file)
    if _DEFAULT_SESSION_FACTORY is not None:
        cached_key, cached_factory = _DEFAULT_SESSION_FACTORY
        if cached_key == db_file:
            return cached_factory

    engine = sa.create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _DEFAULT_SESSION_FACTORY = (db_file, factory)
    return factory


def _reset_default_session_factory_cache() -> None:
    """Test hook — clear the cached sessionmaker."""
    global _DEFAULT_SESSION_FACTORY
    _DEFAULT_SESSION_FACTORY = None


class FxRefreshLoop(CadenceLoop):
    """Daily best-effort refresh of the USD/NIS cache."""

    name = "fx_refresh"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(interval_seconds=86_400),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        factory = self._session_factory or _build_default_session_factory()
        try:
            with factory() as session:
                refreshed = refresh_if_stale(session, currencies=("USD",), max_stale_days=1)
        except Exception as exc:  # noqa: BLE001 — failure isolation; last-known rate stands
            _log.warning("fx_refresh.tick_failed", error=str(exc)[:200])
            return {"error": str(exc)[:200]}
        _log.info("fx_refresh.tick_done", refreshed=bool(refreshed))
        return {"refreshed": bool(refreshed)}


__all__ = ["FxRefreshLoop"]
