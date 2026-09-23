from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from argosy.services import alpha_capture as capture
from argosy.services.jobs import alpha_capture_daily as daily
from argosy.services.research_catalog import complete_item
from argosy.state.models import Base, User
from argosy.state.research_models import ResearchSource, ResearchItem, ResearchClaim, ResearchReviewRequest

NOW = datetime(2026, 9, 22, 17, tzinfo=UTC)


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'research.db'}")
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (User, ResearchSource, ResearchItem, ResearchClaim, ResearchReviewRequest)])
    with Session(engine) as db:
        db.add_all([User(id="a"), User(id="b")]); db.commit()
    @contextmanager
    def sessions():
        with Session(engine, expire_on_commit=False) as db:
            yield db
    monkeypatch.setattr(capture, "research_session", sessions)
    monkeypatch.setattr(daily, "research_session", sessions)
    async def catalog(**kwargs):
        assert kwargs["source"] == "browser_capture"
        assert kwargs["mime_type"] == "application/zip"
        return SimpleNamespace(id=42)
    monkeypatch.setattr("argosy.services.file_catalog.catalog_upload", catalog)
    yield sessions
    engine.dispose()


def page(tmp_path, text="Fresh source thesis", image=""):
    path = tmp_path / "capture.html"
    cards = []
    for day in (22, 21):
        cards.append(f'<div><div>Alpha Report</div><div><p>9-{day}-2026 Alpha Report: Morning Brief</p><p>{text}</p>{image}</div><div><button>Copy Link To This Post</button></div></div>')
    path.write_text("<html><body>" + "".join(cards) + "</body></html>", encoding="utf8")
    return path


def test_parser_dates_and_login_rejection(tmp_path):
    posts = capture.parse_capture(page(tmp_path))
    assert [p.identity for p in posts] == ["report:2026-09-22", "report:2026-09-21"]
    path = tmp_path / "login.html"
    path.write_text("<html>Sign in with Google</html>")
    with pytest.raises(ValueError, match="No Alpha reports"):
        capture.parse_capture(path)


def test_real_saved_page_contract():
    path = Path("scratchpad/alpha_capture_trial/alpha-live-20260922T174101920.html")
    if not path.exists():
        pytest.skip("Private real capture is intentionally not committed")
    posts = capture.parse_capture(path)
    assert len(posts) == 10
    assert sum(len(p.assets) for p in posts) == 20


@pytest.mark.parametrize("src", ["https://example.com/private.png", "../secret.png", "file:///C:/secret.png", "data:image/png;base64,AA==", "blob:example"])
def test_unsafe_unsaved_assets_fail_closed(tmp_path, src):
    (tmp_path / "capture_files").mkdir()
    with pytest.raises((ValueError, FileNotFoundError)):
        capture.parse_capture(page(tmp_path, image=f'<img src="{src}">'))


@pytest.mark.asyncio
async def test_import_idempotency_revisions_history_and_tenants(store, tmp_path):
    path = page(tmp_path)
    first = await capture.ingest_capture(path, user_id="a", now=NOW)
    assert (first["queued"], first["historical"]) == (1, 1)
    again = await capture.ingest_capture(path, user_id="a", now=NOW+timedelta(hours=1))
    assert again["unchanged"] == 2 and again["queued"] == 0
    revised = await capture.ingest_capture(page(tmp_path, text="Changed thesis"), user_id="a", now=NOW)
    assert revised["queued"] == 2
    with store() as db:
        rows = db.scalars(select(ResearchItem)).all()
        assert len(rows) == 4
        assert all(r.published_at is None for r in rows)
        assert all(json.loads(r.analysis_json)["capture"]["catalog_file_id"] == 42 for r in rows)
    other = await capture.ingest_capture(path, user_id="b", now=NOW)
    assert other["queued"] == 1


@pytest.mark.asyncio
async def test_capture_provenance_survives_analysis_and_repeat_has_no_new_clock(store, tmp_path):
    await capture.ingest_capture(page(tmp_path), user_id="a", now=NOW)
    with store() as db:
        item = db.scalar(select(ResearchItem).where(ResearchItem.status == "queued"))
        payload = {"claims": {"speaker_calls": [{"statement": "Standing forecast", "ticker": "XYZ",
            "horizon_days": 3650, "is_reiteration": True, "direction": "bullish"}]}}
        complete_item(db, item, payload, now=NOW)
        db.commit()
        assert json.loads(item.analysis_json)["capture"]["catalog_file_id"] == 42
        assert db.scalar(select(ResearchClaim)).due_at is None


def test_daily_claim_survives_restart_and_is_tenant_scoped(store):
    first, _ = daily.claim_capture(user_id="a", now=NOW)
    second, _ = daily.claim_capture(user_id="a", now=NOW)
    assert first and second is None  # Crash after claim cannot open browser again.
    daily.update_capture(user_id="a", attempt_id=first["attempt_id"], state="failed", error="login")
    assert daily.claim_capture(user_id="a", now=NOW)[0] is None
    assert daily.claim_capture(user_id="b", now=NOW)[0]
    assert daily.claim_capture(user_id="a", now=NOW, explicit_retry=True)[0]
    assert daily.claim_capture(user_id="a", now=NOW+timedelta(days=1))[0]


@pytest.mark.asyncio
async def test_locked_desktop_consumes_no_attempt(store, monkeypatch):
    monkeypatch.setattr(daily, "configuration", lambda: {"enabled": True, "user_id": "a", "google_email": "a@example.com"})
    monkeypatch.setattr(daily, "desktop_available", lambda: False)
    assert (await daily.run_daily(now=NOW))["status"] == "waiting_for_desktop"
    with store() as db:
        state = json.loads(db.scalar(select(ResearchSource)).config_json)
        assert state["capture_state"] == "waiting_for_desktop" and "attempt_day" not in state


def test_reserved_capacity_does_not_expand_general_source_budget(store, monkeypatch):
    from argosy.services import research_worker as worker
    from argosy.services.research_catalog import upsert_source, enqueue
    monkeypatch.setattr(worker, "research_session", store)
    with store() as db:
        for n in range(4):
            source = upsert_source(db, user_id="a", name=str(n), kind="manual", reference=str(n), priority=100)
            enqueue(db, source, external_id=str(n), title="Research", url="", body=str(n), now=NOW)
        alpha = capture.source_row(db, "a")
        enqueue(db, alpha, external_id="browser:report:today", title="Alpha", url=capture.URL, body="source", now=NOW)
        db.commit()
    results = [worker.claim_next(user_id="a", now=NOW) for _ in range(5)]
    assert all(results[:4]) and results[4] is None
    assert results[3][1] == "browser_capture"


@pytest.mark.asyncio
async def test_partial_images_and_origin_date_are_not_full_analysis_or_reset_clock(store, tmp_path):
    await capture.ingest_capture(page(tmp_path), user_id="a", now=NOW)
    with store() as db:
        item = db.scalar(select(ResearchItem).where(ResearchItem.status == "queued"))
        metadata = json.loads(item.analysis_json)
        metadata["capture"]["images"] = 1
        item.analysis_json = json.dumps(metadata)
        complete_item(db, item, {"claims": {"speaker_calls": [{"statement": "Old dated forecast", "ticker": "XYZ",
            "horizon_days": 365, "forecast_origin_date": "2025-01-01", "direction": "bullish"}]}}, now=NOW)
        db.commit()
        assert item.status == "analyzed_partial"
        assert db.scalar(select(ResearchClaim)).due_at is None


def test_paused_source_does_not_open_browser(store):
    with store() as db:
        source = capture.source_row(db, "a")
        source.enabled = False
        db.commit()
    attempt, config = daily.claim_capture(user_id="a", now=NOW)
    assert attempt is None and config["capture_state"] == "disabled"


def test_deferral_and_claim_concurrency_cannot_erase_attempt(store):
    from concurrent.futures import ThreadPoolExecutor
    with store() as db:
        capture.source_row(db, "a")
        db.commit()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(daily.defer_capture, user_id="a", now=NOW) for _ in range(8)]
        futures += [pool.submit(daily.claim_capture, user_id="a", now=NOW) for _ in range(8)]
        results = [f.result() for f in futures]
    claims = [r[0] for r in results if r is not None and r[0] is not None]
    assert len(claims) == 1
    with store() as db:
        config = json.loads(db.scalar(select(ResearchSource)).config_json)
        assert config["attempt_id"] == claims[0]["attempt_id"]
        assert config["capture_state"] == "capturing"
    with pytest.raises(RuntimeError, match="still active"):
        daily.claim_capture(user_id="a", now=NOW, explicit_retry=True)


@pytest.mark.asyncio
async def test_stale_capture_cannot_publish(store, tmp_path):
    daily.claim_capture(user_id="a", now=NOW)
    with pytest.raises(RuntimeError, match="ownership"):
        await capture.ingest_capture(page(tmp_path), user_id="a", now=NOW, attempt_id="stale")
    with store() as db:
        assert db.scalar(select(ResearchItem)) is None


def test_missing_css_dependency_is_not_accepted(tmp_path):
    path = page(tmp_path)
    with pytest.raises(ValueError, match="missing local dependency"):
        capture.validate_saved_dependencies(path, b'<link href="capture_files/missing.css">', {})


@pytest.mark.asyncio
async def test_html_size_checked_before_read(store, tmp_path, monkeypatch):
    path = page(tmp_path)
    monkeypatch.setattr(capture, "MAX_HTML", 20)
    with pytest.raises(ValueError, match="size limit"):
        await capture.ingest_capture(path, user_id="a", now=NOW)


def test_repeated_image_occurrences_bounded(tmp_path):
    folder = tmp_path / "capture_files"
    folder.mkdir()
    from PIL import Image
    Image.new("RGB", (1, 1)).save(folder / "x.png")
    with pytest.raises(ValueError, match="occurrence"):
        capture.parse_capture(page(tmp_path, image='<img src="capture_files/x.png">'*201))


def test_missed_evening_catches_up_next_wake_but_not_twice_today():
    morning = NOW.replace(hour=7)
    assert daily.capture_due({"attempt_day": "2026-09-20"}, morning)
    assert not daily.capture_due({"attempt_day": "2026-09-21"}, morning)
    assert not daily.capture_due({"attempt_day": "2026-09-22"}, NOW)


def test_first_deferred_evening_catches_up_in_morning(store):
    daily.defer_capture(user_id="a", now=NOW)
    with store() as db:
        state = json.loads(db.scalar(select(ResearchSource)).config_json)
    morning = NOW.replace(hour=7) + timedelta(days=1)
    assert daily.capture_due(state, morning)
    daily.claim_capture(user_id="a", now=morning)
    with store() as db:
        state = json.loads(db.scalar(select(ResearchSource)).config_json)
    assert "deferred_day" not in state
    assert not daily.capture_due(state, NOW+timedelta(days=1))


def test_truncated_image_not_committed(tmp_path):
    folder = tmp_path / "capture_files"
    folder.mkdir()
    (folder / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ValueError, match="incomplete or invalid image"):
        capture.parse_capture(page(tmp_path, image='<img src="capture_files/x.png">'))


def test_final_editor_receives_the_readers_actual_portfolio_source():
    from argosy.agents.youtube_analysis import YouTubeSynthesisAgent
    agent = YouTubeSynthesisAgent(user_id="a")
    _, _, sources = agent.build_prompt(transcript_source_id="doc", transcript="report",
        claims_source_id="claims", claims_json="{}", skeptic_source_id="skeptic", skeptic_json="{}",
        portfolio_source_id="portfolio-review", portfolio_json="{}",
        portfolio_context_source_id="argosy:a:portfolio", portfolio_context="Original supplied holdings")
    assert ("argosy:a:portfolio", "Original supplied holdings") in sources
    assert agent.claude_code_force_toolless


def test_asset_budget_prevents_reading_the_next_file(tmp_path, monkeypatch):
    from PIL import Image
    folder = tmp_path / "capture_files"
    folder.mkdir()
    for name in ("one.png", "two.png"):
        Image.new("RGB", (1, 1)).save(folder / name)
    path = page(tmp_path, image='<img src="capture_files/one.png"><img src="capture_files/two.png">')
    monkeypatch.setattr(capture, "MAX_TOTAL", path.stat().st_size + (folder / "one.png").stat().st_size)
    opened = []
    original = Path.open
    def observed_open(self, *args, **kwargs):
        opened.append(self.name)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observed_open)
    with pytest.raises(ValueError, match="size limit"):
        capture.parse_capture(path)
    assert "one.png" in opened and "two.png" not in opened


@pytest.mark.parametrize("css", [
    '<style>@import "missing.css";</style>',
    '<style>.chart{background-image:url(missing.png)}</style>',
    '<div style="background:url(missing.png)">visual</div>',
])
def test_inline_css_cannot_hide_missing_or_uninterpreted_visuals(tmp_path, css):
    with pytest.raises(ValueError, match="missing local dependency|Unsupported CSS visual"):
        capture.validate_saved_dependencies(tmp_path / "x.html", css.encode(), {})


def test_root_relative_fonts_are_external_not_windows_paths(tmp_path):
    result = capture.validate_saved_dependencies(tmp_path / "x.html",
        b'<style>@font-face{src:url(/cf-fonts/example.woff2)}</style>', {})
    assert result["external_resources_not_fetched"] == 1


@pytest.mark.asyncio
async def test_archive_retry_has_identical_catalog_bytes(store, tmp_path, monkeypatch):
    uploads = []
    async def catalog(**kwargs):
        uploads.append(kwargs["raw_bytes"])
        return SimpleNamespace(id=42)
    monkeypatch.setattr("argosy.services.file_catalog.catalog_upload", catalog)
    path = page(tmp_path)
    await capture.ingest_capture(path, user_id="a", now=NOW)
    await capture.ingest_capture(path, user_id="a", now=NOW+timedelta(hours=2))
    assert uploads[0] == uploads[1]


@pytest.mark.asyncio
async def test_cli_uses_registered_receipts_and_passes_explicit_retry(engine, monkeypatch):
    from scripts.run_alpha_capture import run_registered
    from argosy.state import db as db_mod
    from argosy.state.models import JobRun
    monkeypatch.setattr(daily, "configuration", lambda: {"enabled": True})
    calls = []
    async def run(**kwargs):
        calls.append(kwargs["explicit_retry"])
        return {"status": "already_captured"}
    monkeypatch.setattr(daily, "run_daily", run)
    result = await run_registered(explicit_retry=True)
    assert calls == [True]
    assert result["job_status"] == "ok"
    assert result["result"]["status"] == "already_captured"
    async with db_mod.get_session() as session:
        row = await session.get(JobRun, result["job_run_id"])
        assert row.job_name == "alpha_capture_daily"
        assert row.triggered_by == "cli:alpha-capture"


@pytest.mark.asyncio
async def test_cli_failure_is_recorded_not_hidden(engine, monkeypatch):
    from scripts.run_alpha_capture import run_registered
    from argosy.state import db as db_mod
    from argosy.state.models import JobRun
    monkeypatch.setattr(daily, "configuration", lambda: {"enabled": True})
    async def broken(**kwargs):
        raise ValueError("Capture failed before import")
    monkeypatch.setattr(daily, "run_daily", broken)
    with pytest.raises(ValueError, match="Capture failed"):
        await run_registered()
    async with db_mod.get_session() as session:
        row = (await session.execute(select(JobRun).where(JobRun.job_name == "alpha_capture_daily"))).scalar_one()
        assert row.status == "error"
        assert "Capture failed" in row.error_message
