"""Tests for `argosy admin catalog-backfill` (Wave A)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from argosy.cli.catalog_backfill import (
    _backfill_plan_versions,
    _backfill_uploads_dir,
)
from argosy.state import db as db_mod
from argosy.state.models import PlanVersion, User, UserFile


@pytest.mark.asyncio
async def test_backfill_walks_legacy_layout(argosy_home_db):
    """Files under <home>/uploads/<user>/<turn_uuid>/<file> get cataloged."""
    home = argosy_home_db
    legacy = home / "uploads" / "ariel" / "abcdef0123456789abcdef0123456789"
    legacy.mkdir(parents=True)
    (legacy / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (legacy / "plan.md").write_bytes(b"# Old plan\n")

    inserted, skipped = await _backfill_uploads_dir(user_filter=None, dry_run=False)
    assert inserted == 2

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(select(UserFile).where(UserFile.user_id == "ariel"))
        ).scalars().all()
    names = {r.original_name for r in rows}
    assert names == {"shot.png", "plan.md"}
    # Turn UUID should be extracted from the legacy path.
    for r in rows:
        assert r.turn_uuid == "abcdef0123456789abcdef0123456789"


@pytest.mark.asyncio
async def test_backfill_idempotent(argosy_home_db):
    """Running backfill twice doesn't double-insert."""
    legacy = argosy_home_db / "uploads" / "ariel" / ("deadbeef" * 4)
    legacy.mkdir(parents=True)
    (legacy / "x.txt").write_bytes(b"once")

    a_ins, _ = await _backfill_uploads_dir(user_filter=None, dry_run=False)
    b_ins, b_skip = await _backfill_uploads_dir(user_filter=None, dry_run=False)
    assert a_ins == 1
    assert b_ins == 0
    assert b_skip == 1


@pytest.mark.asyncio
async def test_backfill_user_filter(argosy_home_db):
    """--user-id filter restricts to one user."""
    # Add bob via the same async engine the fixture initialized.
    async with db_mod.get_session() as session:
        session.add(User(id="bob", plan="free"))
        await session.commit()

    (argosy_home_db / "uploads" / "ariel" / "t1").mkdir(parents=True)
    (argosy_home_db / "uploads" / "ariel" / "t1" / "a.txt").write_bytes(b"a")
    (argosy_home_db / "uploads" / "bob" / "t2").mkdir(parents=True)
    (argosy_home_db / "uploads" / "bob" / "t2" / "b.txt").write_bytes(b"b")

    inserted, _ = await _backfill_uploads_dir(user_filter="ariel", dry_run=False)
    assert inserted == 1


@pytest.mark.asyncio
async def test_backfill_plan_versions_links_source_file_id(argosy_home_db):
    """A plan_versions row with source_path matching a cataloged user_files
    row gets source_file_id linked.
    """
    # Catalog a plan markdown via the helper.
    from argosy.services.file_catalog import catalog_upload
    dto = await catalog_upload(
        user_id="ariel",
        raw_bytes=b"# My plan",
        original_name="my-plan.md",
        mime_type="text/markdown",
        kind="plan_markdown",
        source="intake_upload",
    )

    # Insert a legacy plan_versions row WITHOUT source_file_id (simulating
    # a row that predates Wave A).
    async with db_mod.get_session() as session:
        pv = PlanVersion(
            user_id="ariel",
            version_label="legacy",
            source_path="my-plan.md",
            raw_markdown="# My plan",
        )
        session.add(pv)
        await session.commit()
        await session.refresh(pv)
        pv_id = pv.id

    matched, _ = await _backfill_plan_versions(user_filter=None, dry_run=False)
    assert matched == 1

    async with db_mod.get_session() as session:
        from sqlalchemy import select as _select
        pv = (
            await session.execute(_select(PlanVersion).where(PlanVersion.id == pv_id))
        ).scalar_one()
        assert pv.source_file_id == dto.id
