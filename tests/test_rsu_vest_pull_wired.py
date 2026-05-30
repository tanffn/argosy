"""Wiring test: dropping a Schwab Equity Awards CSV under
``$ARGOSY_EXPENSE_SAMPLES_ROOT`` actually flows into ``rsu_vest_events``.

This is the missing production-caller cover for
``argosy/services/rsu_vest_ingest.py::ingest_schwab_vest_events``. The
function itself is exercised by ``tests/test_rsu_vest_ingest.py``; THIS
test asserts the new ``argosy/services/rsu_vest_pull.py`` helper +
``monthly_cycle._real_rsu_pull`` glue actually pick up a freshly-dropped
CSV and persist rows.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.rsu_vest_pull import (
    discover_schwab_csvs,
    ingest_samples_root,
)
from argosy.state.models import Base, RsuVestEvent, User


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "portfolio_ingest_schwab"
    / "EquityAwardsCenter_Transactions_20260529.csv"
)


@pytest.fixture
def scratch_session_factory(tmp_path):
    """Self-contained SQLite + seeded user 'ariel'.

    Returns the sessionmaker so the wiring helper can open / close
    one session per CSV exactly like production.
    """
    db_path = tmp_path / "rsu_vest_pull.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)

    # Seed the user via a throwaway session.
    seed = SF()
    seed.add(User(id="ariel", plan="free"))
    seed.commit()
    seed.close()

    try:
        yield SF
    finally:
        engine.dispose()


@pytest.fixture
def samples_root_with_fixture(tmp_path):
    """A scratch ``$ARGOSY_EXPENSE_SAMPLES_ROOT`` shape with the fixture
    dropped under a year directory the way the user-guide describes.
    """
    root = tmp_path / "samples_root"
    schwab_dir = root / "2026" / "Schwab"
    schwab_dir.mkdir(parents=True)
    target = schwab_dir / FIXTURE_PATH.name
    shutil.copyfile(FIXTURE_PATH, target)
    return root


# ---------------------------------------------------------------------------
# discover_schwab_csvs — recursive scan
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_finds_fixture_under_year_schwab(self, samples_root_with_fixture):
        csvs = discover_schwab_csvs(samples_root_with_fixture)
        assert len(csvs) == 1
        assert csvs[0].name == FIXTURE_PATH.name

    def test_finds_fixture_at_root(self, tmp_path):
        """Per the user-guide: 'drop a Schwab Equity Awards CSV ANYWHERE
        under $ARGOSY_EXPENSE_SAMPLES_ROOT'."""
        root = tmp_path / "samples_root"
        root.mkdir()
        shutil.copyfile(FIXTURE_PATH, root / FIXTURE_PATH.name)
        csvs = discover_schwab_csvs(root)
        assert len(csvs) == 1

    def test_ignores_non_matching_filenames(self, tmp_path):
        root = tmp_path / "samples_root"
        root.mkdir()
        (root / "some_other.csv").write_text("not a schwab file")
        (root / "transactions.csv").write_text("also not")
        csvs = discover_schwab_csvs(root)
        assert csvs == []

    def test_skips_venv_and_node_modules(self, tmp_path):
        """Scratch dirs must not shadow real exports."""
        root = tmp_path / "samples_root"
        (root / ".venv" / "lib").mkdir(parents=True)
        shutil.copyfile(
            FIXTURE_PATH,
            root / ".venv" / "lib" / FIXTURE_PATH.name,
        )
        (root / "node_modules" / "pkg").mkdir(parents=True)
        shutil.copyfile(
            FIXTURE_PATH,
            root / "node_modules" / "pkg" / FIXTURE_PATH.name,
        )
        csvs = discover_schwab_csvs(root)
        assert csvs == []


# ---------------------------------------------------------------------------
# ingest_samples_root — end-to-end against scratch DB
# ---------------------------------------------------------------------------


class TestIngestSamplesRoot:
    def test_ingest_drops_rows_into_rsu_vest_events(
        self, scratch_session_factory, samples_root_with_fixture
    ):
        """Drop fixture into a scratch samples root + run the helper +
        assert rsu_vest_events rows landed.
        """
        results = ingest_samples_root(
            user_id="ariel",
            session_factory=scratch_session_factory,
            samples_root=samples_root_with_fixture,
        )
        assert len(results) == 1
        r = results[0]
        assert "error" not in r
        assert r["parsed"] == 74
        assert r["inserted"] == 74
        assert r["duplicates"] == 0

        # Inspect the DB directly.
        verify = scratch_session_factory()
        try:
            count = verify.query(RsuVestEvent).count()
            assert count == 74
            # Sanity: source_file is recorded.
            sample = verify.query(RsuVestEvent).first()
            assert sample.source_file.endswith(FIXTURE_PATH.name)
        finally:
            verify.close()

    def test_second_run_is_idempotent(
        self, scratch_session_factory, samples_root_with_fixture
    ):
        """Re-running the helper on the same root must not duplicate
        rows — the table's UNIQUE constraint + ingest_schwab_vest_events'
        per-row exists-check handles this.
        """
        first = ingest_samples_root(
            user_id="ariel",
            session_factory=scratch_session_factory,
            samples_root=samples_root_with_fixture,
        )
        second = ingest_samples_root(
            user_id="ariel",
            session_factory=scratch_session_factory,
            samples_root=samples_root_with_fixture,
        )
        assert first[0]["inserted"] == 74
        assert second[0]["inserted"] == 0
        assert second[0]["duplicates"] == 74

        verify = scratch_session_factory()
        try:
            assert verify.query(RsuVestEvent).count() == 74
        finally:
            verify.close()

    def test_missing_samples_root_skips_gracefully(
        self, scratch_session_factory, tmp_path
    ):
        """Unconfigured samples root must NOT raise — return empty list."""
        ghost = tmp_path / "does_not_exist"
        results = ingest_samples_root(
            user_id="ariel",
            session_factory=scratch_session_factory,
            samples_root=ghost,
        )
        assert results == []

    def test_unset_env_skips_when_no_override(
        self, scratch_session_factory, monkeypatch
    ):
        """When samples_root is not supplied and the env var isn't set,
        the helper logs + returns an empty list (no exception)."""
        monkeypatch.delenv("ARGOSY_EXPENSE_SAMPLES_ROOT", raising=False)
        results = ingest_samples_root(
            user_id="ariel",
            session_factory=scratch_session_factory,
            samples_root=None,
        )
        assert results == []

    def test_env_var_drives_discovery_when_no_override(
        self,
        scratch_session_factory,
        samples_root_with_fixture,
        monkeypatch,
    ):
        """The production path: env var alone wires the scanner."""
        monkeypatch.setenv(
            "ARGOSY_EXPENSE_SAMPLES_ROOT",
            str(samples_root_with_fixture),
        )
        results = ingest_samples_root(
            user_id="ariel",
            session_factory=scratch_session_factory,
            samples_root=None,
        )
        assert len(results) == 1
        assert results[0]["inserted"] == 74

    def test_bad_csv_does_not_abort_loop(
        self, scratch_session_factory, samples_root_with_fixture, tmp_path,
        monkeypatch,
    ):
        """One malformed CSV must not prevent ingest of a good one.

        We force the per-file try/except path by monkeypatching the
        ingest function so that calling it on a sentinel filename
        raises. The real fixture under samples_root_with_fixture is
        still ingested successfully.
        """
        # Drop a sentinel Schwab-shaped filename alongside the real one.
        sentinel = samples_root_with_fixture / "EquityAwardsCenter_Transactions_99999999.csv"
        sentinel.write_text("placeholder")

        from argosy.services import rsu_vest_pull as pull_mod
        real_ingest = pull_mod.ingest_schwab_vest_events

        def _wrapped(*, session, user_id, csv_path):
            if csv_path.name == sentinel.name:
                raise RuntimeError("simulated parser failure")
            return real_ingest(session=session, user_id=user_id, csv_path=csv_path)

        monkeypatch.setattr(pull_mod, "ingest_schwab_vest_events", _wrapped)

        results = ingest_samples_root(
            user_id="ariel",
            session_factory=scratch_session_factory,
            samples_root=samples_root_with_fixture,
        )
        # Two files attempted.
        assert len(results) == 2
        # Exactly one error + exactly one success.
        errors = [r for r in results if "error" in r]
        successes = [r for r in results if "error" not in r]
        assert len(errors) == 1
        assert len(successes) == 1
        assert successes[0]["inserted"] == 74


# ---------------------------------------------------------------------------
# monthly_cycle._real_rsu_pull — the async wrapper the loop uses by default
# ---------------------------------------------------------------------------


class TestMonthlyCycleDefault:
    def test_real_rsu_pull_is_the_default(self):
        """The default ``rsu_vest_pull`` on ``MonthlyCycleLoop.__init__``
        is the real wired callable, not the no-op placeholder.
        """
        from argosy.orchestrator.loops import monthly_cycle as mc
        from argosy.orchestrator.loops.base import LoopSchedule

        loop = mc.MonthlyCycleLoop(
            schedule=LoopSchedule(cron="0 8 1 * *"),
            user_id="ariel",
        )
        # The bound default must be _real_rsu_pull, not _noop_rsu_pull.
        assert loop._rsu_vest_pull is mc._real_rsu_pull
        assert loop._rsu_vest_pull is not mc._noop_rsu_pull

    def test_real_rsu_pull_invokes_ingest_samples_root(self, monkeypatch):
        """The async wrapper must bridge into ``ingest_samples_root`` via
        ``asyncio.to_thread`` and propagate its result list.
        """
        from argosy.orchestrator.loops import monthly_cycle as mc

        captured: dict = {}

        def _fake_ingest(user_id, *, session_factory=None, samples_root=None):
            captured["user_id"] = user_id
            captured["called"] = True
            return [{
                "source_file": "stub.csv",
                "parsed": 5,
                "inserted": 5,
                "duplicates": 0,
            }]

        monkeypatch.setattr(
            "argosy.services.rsu_vest_pull.ingest_samples_root",
            _fake_ingest,
        )
        out = asyncio.run(mc._real_rsu_pull("ariel"))
        assert captured["user_id"] == "ariel"
        assert captured["called"] is True
        assert out == [{
            "source_file": "stub.csv",
            "parsed": 5,
            "inserted": 5,
            "duplicates": 0,
        }]
