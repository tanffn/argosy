"""Finish deterministic scan-state reconciliation after an interrupted funnel."""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from argosy.config import get_settings
from argosy.state.db import create_sync_engine
from argosy.state.models import Prediction, ScanState


def main() -> None:
    url = str(get_settings().database_url).replace("+aiosqlite", "")
    engine = create_sync_engine(url)
    with Session(engine) as session:
        observed_at = session.execute(select(func.max(Prediction.event_at)).where(
            Prediction.user_id == "ariel",
            Prediction.source == "signal_stream:radar_observation",
            Prediction.timeframe_days == 180,
        )).scalar_one_or_none()
        if observed_at is None:
            raise SystemExit("No radar observation clocks exist; nothing to reconcile")
        current = {
            str(ticker).upper()
            for ticker in session.execute(select(Prediction.ticker).where(
                Prediction.user_id == "ariel",
                Prediction.source == "signal_stream:radar_observation",
                Prediction.timeframe_days == 180,
                Prediction.event_at == observed_at,
            )).scalars()
            if ticker
        }
        stale = session.execute(select(ScanState).where(
            ScanState.user_id == "ariel",
            ScanState.status == "active",
            ScanState.ticker.not_in(current),
        )).scalars().all()
        for row in stale:
            row.status = "dropped"
        session.commit()
        print(json.dumps({
            "observed_at": observed_at.isoformat(),
            "current_active": len(current),
            "stale_marked_dropped": sorted(row.ticker for row in stale),
        }))
    engine.dispose()


if __name__ == "__main__":
    main()
