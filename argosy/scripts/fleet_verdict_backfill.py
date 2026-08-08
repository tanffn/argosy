"""Backfill fleet-verdict predictions from existing verdicts.

Idempotent. Includes superseded and rejected-proposal verdicts (scored as
what the fleet said). Resolves entry prices as-of the verdict timestamp
by default.

Example::

    .venv/Scripts/python.exe -m argosy.scripts.fleet_verdict_backfill \\
        --db-url sqlite:///D:/tmp/argosy_fixture.db \\
        --user-id ariel
"""
from __future__ import annotations

import argparse
import json
import sys

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.predictions.event_time_price import (
    as_of_resolver_for_backfill,
)
from argosy.services.predictions.fleet_verdict_backfill import (
    backfill_fleet_verdict_predictions,
)


def _parse_entry_prices(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    out: dict[str, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit(f"bad --entry-prices token: {part!r}")
        ticker, px = part.split("=", 1)
        out[ticker.strip().upper()] = float(px.strip())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-url",
        required=True,
        help="SQLAlchemy URL (required — refuse to default to live DB)",
    )
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument(
        "--entry-prices",
        default=None,
        help="Optional comma list TICKER=price (overrides as-of resolver)",
    )
    parser.add_argument(
        "--subjects",
        default=None,
        help="Optional comma list of subjects to limit the scan",
    )
    parser.add_argument(
        "--no-resolver",
        action="store_true",
        help="Do not use the default as-of price resolver (fixture-only)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--i-know-this-is-live",
        action="store_true",
        help="Required to proceed when --db-url looks like db/argosy.db",
    )
    args = parser.parse_args(argv)

    url_norm = args.db_url.replace("\\", "/").lower()
    looks_live = "argosy.db" in url_norm and "fixture" not in url_norm
    if looks_live and not args.i_know_this_is_live:
        print(
            "Refusing db-url that looks like the live ledger. "
            "Pass a fixture URL, or --i-know-this-is-live.",
            file=sys.stderr,
        )
        return 2

    engine = sa.create_engine(args.db_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        only = (
            {s.strip().upper() for s in args.subjects.split(",") if s.strip()}
            if args.subjects
            else None
        )
        resolver = None if args.no_resolver else as_of_resolver_for_backfill
        summary = backfill_fleet_verdict_predictions(
            session,
            args.user_id,
            entry_prices=_parse_entry_prices(args.entry_prices),
            price_resolver=resolver,
            only_subjects=only,
        )
        payload = summary.to_dict()
        if args.dry_run:
            session.rollback()
            payload = {"dry_run": True, **payload}
        else:
            session.commit()
        print(json.dumps(payload, indent=2))
        # A run that scanned rows but wrote nothing and skipped everything
        # without a resolver is a configuration error — do not report success.
        if (
            summary.scanned > 0
            and summary.written == 0
            and summary.versioned == 0
            and resolver is None
            and not _parse_entry_prices(args.entry_prices)
        ):
            print(
                "ERROR: no rows written and no price resolver/entry-prices; "
                "refusing success.",
                file=sys.stderr,
            )
            return 1
    finally:
        session.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
