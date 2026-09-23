"""Audit live prediction clocks; optionally replay saved analyses on a disposable DB copy.

No live writes, LLM calls, credential access or report downloads. Replay exercises
the production fan-out, writer, constraints and evaluator's due selector against
a SQLite backup, not a replacement writer or fabricated analyst response.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def audit(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        "SELECT timeframe_days, evaluation_method, "
        "ROUND(julianday(evaluation_due_at)-julianday(event_at)), COUNT(*) "
        "FROM predictions WHERE source='discord_alpha_report' GROUP BY 1,2,3"
    ).fetchall()
    return [dict(zip(("claimed_days", "method", "scheduled_days", "count"), row, strict=True))
            for row in rows]


def replay(path: Path) -> dict:
    from pydantic import TypeAdapter
    from sqlalchemy import create_engine, event, func, select, text
    from sqlalchemy.orm import Session

    from argosy.agents.alpha_report_analyst import AlphaReportAnalysis as AnalysisDTO
    from argosy.services.alpha_report_analyst_runner import _fan_out_predictions
    from argosy.services.predictions.evaluator import find_due_predictions
    from argosy.state.models import AlphaReportAnalysis, NewsSignal, Prediction

    engine = create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        with Session(engine) as session:
            # Only the disposable backup is writable. Free the source dedup keys
            # there without deleting its historical predictions or outcomes.
            baseline_id = session.scalar(select(func.max(Prediction.id))) or 0
            session.execute(text(
                "UPDATE predictions SET message_id='horizon-replay-old:' || id "
                "WHERE source='discord_alpha_report'"
            ))
            analyses = session.scalars(select(AlphaReportAnalysis)).all()
            if not analyses:
                raise RuntimeError("No saved Alpha analyses; replay cannot prove anything")
            expected = 0
            for row in analyses:
                dto = TypeAdapter(AnalysisDTO).validate_python({
                    "macro_tone": row.macro_tone,
                    "macro_tone_confidence": row.macro_tone_confidence,
                    "key_themes": json.loads(row.key_themes),
                    "summary_rationale": row.summary_rationale,
                    "ticker_signals": json.loads(row.ticker_signals_json),
                    "structural_picks": json.loads(row.structural_picks_json),
                    "cautions": json.loads(row.cautions_json),
                    "index_targets": json.loads(row.index_targets_json),
                    "confidence_overall": row.confidence_overall,
                })
                expected += len({(s.ticker.upper(), "signal") for s in dto.ticker_signals}
                                | {(p.ticker.upper(), "pick") for p in dto.structural_picks})
                signal = session.get(NewsSignal, row.news_signal_id)
                for _ in range(2):  # Real production dedup, same persisted inputs.
                    _fan_out_predictions(
                        session, user_id=row.user_id, analysis_row=row,
                        analysis=dto, event_at=signal.received_at,
                    )
            session.commit()
            predictions = session.scalars(
                select(Prediction).where(Prediction.id > baseline_id)
            ).all()
            assert len(predictions) == expected, (len(predictions), expected)
            horizons = {}
            representatives = {}
            for prediction in predictions:
                days = prediction.timeframe_days
                assert prediction.evaluation_method == f"fixed_lookahead_{days}d"
                assert prediction.evaluation_due_at == prediction.event_at + timedelta(days=days)
                horizons[days] = horizons.get(days, 0) + 1
                representatives.setdefault(days, prediction)
            total = session.scalar(select(func.count()).select_from(Prediction))
            for prediction in representatives.values():
                due = prediction.evaluation_due_at
                before = {p.id for p in find_due_predictions(
                    session, now=due - timedelta(seconds=1), batch_size=total + 1)}
                at_due = {p.id for p in find_due_predictions(
                    session, now=due, batch_size=total + 1)}
                assert prediction.id not in before
                assert prediction.id in at_due
            return {"saved_analyses_replayed": len(analyses),
                    "predictions_written": len(predictions), "horizons": horizons,
                    "duplicate_second_pass": 0, "due_boundary_checks": len(representatives),
                    "live_db_modified": False, "historical_scores_repaired": False}
    finally:
        engine.dispose()


def preview_repair(path: Path, *, migration_roundtrip: bool = False) -> dict:
    """Migrate and repair only the disposable copy, using production code."""
    from argparse import Namespace

    from alembic.config import Config
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session

    from alembic import command
    from argosy.services.predictions.clock_repair import repair_alpha_prediction_horizons
    from argosy.services.predictions.evaluator import find_due_predictions
    from argosy.services.predictions.outcomes import authoritative_outcomes
    from argosy.services.predictions.reliability import (
        get_source_reliability,
        invalidate_reliability_cache,
    )
    from argosy.state.models import AuditLog, Prediction, PredictionOutcome

    config = Config(str(ROOT / "alembic.ini"))
    config.cmd_opts = Namespace(x=[f"db_url=sqlite+aiosqlite:///{path.as_posix()}"])
    command.upgrade(config, "head")
    # Exercise downgrade/re-upgrade only on this generated disposable copy.
    if migration_roundtrip:
        command.downgrade(config, "0119_private_discord_advisor")
        command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with Session(engine) as session:
            rows = session.scalars(select(Prediction).where(
                Prediction.source == "discord_alpha_report"
            )).all()
            outcome_ids = set(session.scalars(select(PredictionOutcome.id)))
            audits_before = session.scalar(select(func.count()).select_from(AuditLog))
            result = repair_alpha_prediction_horizons(session)
            session.commit()
            assert repair_alpha_prediction_horizons(session) == {"repaired": 0}
            assert outcome_ids == set(session.scalars(select(PredictionOutcome.id)))
            assert session.scalar(select(func.count()).select_from(AuditLog)) == audits_before + result["repaired"]
            selected = authoritative_outcomes(session, rows)
            long_rows = [row for row in rows if row.timeframe_days == 180]
            assert all(selected[row.id][1].evaluation_method.startswith("fixed_lookahead_180d")
                       for row in long_rows if row.id in selected)
            invalidate_reliability_cache()
            reliability = get_source_reliability(session, "ariel", source="discord_alpha_report")
            now = datetime.now(UTC)
            total = session.scalar(select(func.count()).select_from(Prediction))
            due_ids = {row.id for row in find_due_predictions(session, now=now, batch_size=total + 1)}
            pending_long = [row for row in long_rows if row.id not in selected]
            for row in pending_long:
                due = row.evaluation_due_at.replace(tzinfo=row.evaluation_due_at.tzinfo or UTC)
                assert (row.id in due_ids) == (due <= now and row.archived == 0)
            return {**result, "audit_rows_added": result["repaired"],
                    "migration_roundtrip": migration_roundtrip,
                    "historical_outcomes_preserved": len(outcome_ids),
                    "alpha_current_outcomes": len(selected),
                    "alpha_current_scores": sum(o.outcome_kind != "unparseable" for _, o, _ in selected.values()),
                    "alpha_current_unscorable": sum(o.outcome_kind == "unparseable" for _, o, _ in selected.values()),
                    "alpha_reliability_scores": sum(r.scored_predictions for r in reliability),
                    "long_horizon_pending": len(pending_long),
                    "long_horizon_due_now": sum(row.id in due_ids for row in pending_long),
                    "second_repair_changes": 0, "live_db_modified": False}
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "db/argosy.db")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--repair-preview", action="store_true")
    parser.add_argument("--migration-roundtrip", action="store_true")
    args = parser.parse_args()
    connection = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        print(json.dumps({"live_audit": audit(connection)}), flush=True)
        if args.replay or args.repair_preview:
            # Generated private temp directory; never remove a user-supplied path.
            with tempfile.TemporaryDirectory(prefix="argosy-horizon-replay-") as folder:
                path = Path(folder) / "replay.db"
                target = sqlite3.connect(path)
                try:
                    connection.backup(target, pages=1024)
                finally:
                    target.close()
                if args.repair_preview:
                    print(json.dumps({"repair_preview": preview_repair(
                        path, migration_roundtrip=args.migration_roundtrip)}), flush=True)
                if args.replay:
                    print(json.dumps({"replay": replay(path)}), flush=True)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
