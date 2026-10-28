"""Once-daily, interactive-session Alpha capture with a crash-durable claim."""
from __future__ import annotations

import asyncio
import ctypes
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import struct
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import text

from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.alpha_capture import ingest_capture, research_session, source_row
from argosy.services.jobs.registry import JobMetadata

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "configs" / "alpha_capture.json"
ZONE = ZoneInfo("Asia/Jerusalem")


def desktop_interrupted(message: str) -> bool:
    """Our own capture-script protocol plus its pre-protocol receipt wording."""
    return any(marker in message for marker in (
        "[DESKTOP_INTERRUPTED]", "Another window took focus; capture stopped",
        "Desktop locked during capture; attempt consumed",
    ))


def interrupted_summary():
    return {"status": "error", "failure": {"code": "desktop_interrupted", "retryable": False},
            "reason": "The desktop became unavailable or another window kept focus; no report was imported.",
            "automatic_reopening": False,
            "next_capture": "Next daily 18:00 Asia/Jerusalem attempt (or catch-up after unlock)",
            "action": "No credentials need changing. Automatic reopening today is suppressed; an explicit retry can capture sooner."}


def configuration():
    if not CONFIG.is_file():
        return {"enabled": False}
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if config.get("enabled") and (not config.get("user_id") or not config.get("google_email")):
        raise ValueError("Alpha capture requires its owner and authorized Google email")
    return config


def _session_is_unlocked(header: bytes, session_id: int) -> bool:
    # WTSINFOEXW: DWORD Level, 8-byte-aligned level-1 union. Only inspect
    # the non-identifying prefix; never copy its account/domain fields.
    if len(header) < 20:
        return False
    level = struct.unpack_from('<I', header)[0]
    sid, state, flags = struct.unpack_from('<IIi', header, 8)
    return level == 1 and sid == session_id and state == 0 and flags == 1


def desktop_available():
    if os.name != "nt":
        return False
    from ctypes import wintypes
    session = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return False
    wts = ctypes.windll.wtsapi32
    wts.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    pointer, size = ctypes.c_void_p(), wintypes.DWORD()
    if not wts.WTSQuerySessionInformationW(None, session.value, 25,
                                         ctypes.byref(pointer), ctypes.byref(size)):
        return False
    try:
        if size.value < 20 or not pointer.value or not _session_is_unlocked(
                ctypes.string_at(pointer, 20), session.value):
            return False
    finally:
        wts.WTSFreeMemory(pointer)
    # SwitchDesktop alone can report success while the WTS session is locked.
    native = ctypes.windll.user32
    native.OpenInputDesktop.restype = ctypes.c_void_p
    native.SwitchDesktop.argtypes = [ctypes.c_void_p]
    native.CloseDesktop.argtypes = [ctypes.c_void_p]
    desktop = native.OpenInputDesktop(0, False, 0x100)
    if not desktop:
        return False
    try:
        return bool(native.SwitchDesktop(desktop))
    finally:
        native.CloseDesktop(desktop)


def capture_due(state, now):
    local = now.astimezone(ZONE)
    if state.get("attempt_day") == local.date().isoformat():
        return False
    if local.hour >= 18:
        return True
    # Catch up a missed prior evening after a longer sleep. This consumes
    # today's one automatic capture, so 18:00 cannot open Chrome a second time.
    previous = state.get("attempt_day")
    deferred = state.get("deferred_day")
    # Once due, a pre-browser lock deferral stays eligible across repeated
    # morning wake-ups; updating its date must not postpone it until 18:00.
    return bool((previous and previous < (local.date() - timedelta(days=1)).isoformat()) or
                (deferred and deferred <= local.date().isoformat()))


def claim_capture(*, user_id, now, explicit_retry=False):
    day = now.astimezone(ZONE).date().isoformat()
    with research_session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        source = source_row(session, user_id)
        config = json.loads(source.config_json or "{}")
        if not source.enabled:
            return None, {"capture_state": "disabled"}
        if explicit_retry and config.get("capture_state") == "capturing":
            started = datetime.fromisoformat(config["attempt_at"])
            if now - started < timedelta(minutes=10):
                raise RuntimeError("A capture attempt is still active; explicit retry cannot replace it")
        if config.get("attempt_day") == day and not explicit_retry:
            return None, {**config, "last_error": source.last_error}
        attempt = {"attempt_day": day, "attempt_id": str(uuid.uuid4()),
                   "attempt_at": now.isoformat(), "capture_state": "capturing",
                   "explicit_retry": explicit_retry}
        config.update(attempt)
        config.pop("deferred_day", None)
        source.config_json = json.dumps(config)
        source.last_error = None
        session.commit()
        return attempt, config


def defer_capture(*, user_id, now):
    with research_session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        source = source_row(session, user_id)
        state = json.loads(source.config_json or "{}")
        if state.get("attempt_day") != now.astimezone(ZONE).date().isoformat():
            state["capture_state"] = "waiting_for_desktop"
            state["deferred_day"] = now.astimezone(ZONE).date().isoformat()
            source.config_json = json.dumps(state)
        session.commit()


def update_capture(*, user_id, attempt_id, state, error=None, browser=None):
    with research_session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        source = source_row(session, user_id)
        config = json.loads(source.config_json or "{}")
        if config.get("attempt_id") != attempt_id:
            raise RuntimeError("Capture ownership changed; refusing stale update")
        config["capture_state"] = state
        if browser is not None:
            # Keep a durable, credential-free receipt before lengthy fleet work.
            config["last_browser"] = {key: browser.get(key) for key in
                ("status", "google_account_selected", "visible_report_headings")}
            config["last_browser"].update(attempt_id=attempt_id, completed_at=datetime.now(UTC).isoformat())
        source.config_json = json.dumps(config)
        source.last_error = error
        session.commit()


def defer_unopened_capture(*, user_id, attempt_id, now, error):
    """Release only our claim when the worker proves no browser was opened.

    A lock between the Python preflight and the hidden worker must not consume
    the day's capture. Mid-capture interruptions retain their once-day guard.
    """
    with research_session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        source = source_row(session, user_id)
        state = json.loads(source.config_json or "{}")
        if state.get("attempt_id") != attempt_id or state.get("capture_state") != "capturing":
            raise RuntimeError("Capture ownership changed; refusing stale deferral")
        state.update(capture_state="waiting_for_desktop", deferred_day=now.astimezone(ZONE).date().isoformat())
        state.pop("attempt_day", None)
        source.config_json = json.dumps(state)
        source.last_error = error
        session.commit()


def capture_browser(path, email):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
        str(ROOT / "scripts/capture_meetkevin_alpha.ps1"), "-OutputPath", str(path),
        "-GoogleEmail", email], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        # No browser contents or account list are logged; only our script errors.
        raise RuntimeError((result.stderr or "Chrome capture failed")[:1800])
    return json.loads(result.stdout.strip())


async def run_daily(*, now=None, explicit_retry=False):
    now = now or datetime.now(UTC)
    config = configuration()
    if not config.get("enabled"):
        return {"status": "disabled"}
    if not explicit_retry and now.astimezone(ZONE).hour < 18:
        def read_state():
            with research_session() as session:
                return json.loads(source_row(session, config["user_id"]).config_json or "{}")
        state = await asyncio.to_thread(read_state)
        if not capture_due(state, now):
            return {"status": "not_due", "next_capture": "18:00 Asia/Jerusalem"}
    if not desktop_available():
        await asyncio.to_thread(defer_capture, user_id=config["user_id"], now=now)
        return {"status": "waiting_for_desktop", "reason": "Capture deferred until Windows is unlocked; no browser opened"}
    user_id = config["user_id"]
    attempt, previous = await asyncio.to_thread(claim_capture, user_id=user_id, now=now, explicit_retry=explicit_retry)
    if attempt is None:
        state = previous.get("capture_state")
        if state == "disabled":
            return {"status": "disabled"}
        if state == "waiting_for_login":
            return {"status": "waiting_for_login", "reason": "Complete login in Chrome, then explicitly retry the capture."}
        if state == "interrupted" or (state == "failed" and desktop_interrupted(previous.get("last_error") or "")):
            return interrupted_summary()
        if state in {"failed", "capturing"}:
            return {"status": "error", "reason": "Today's capture did not finish. Inspect the open browser and retry explicitly; automatic reopening is suppressed."}
        return {"status": "already_captured", "capture": previous.get("last_capture")}
    folder = ROOT / "runtime" / "alpha_capture"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("alpha-" + attempt["attempt_id"] + ".html")
    try:
        from argosy.services.chat_advisor.progress import record_agent_progress
        await record_agent_progress(agent="alpha_capture", state="started",
            detail="Checking the Alpha page and Google account selection in Chrome", correlation_id="alpha_capture")
        browser = await asyncio.to_thread(capture_browser, path, config["google_email"])
        await record_agent_progress(agent="alpha_capture", state="running",
            detail="Page saved; validating report text and images before import", correlation_id="alpha_capture")
        result = await ingest_capture(path, user_id=user_id, now=now, attempt_id=attempt["attempt_id"])
        await asyncio.to_thread(update_capture, user_id=user_id, attempt_id=attempt["attempt_id"], state="captured", browser=browser)
    except Exception as exc:
        if "[DESKTOP_DEFERRED]" in str(exc):
            await asyncio.to_thread(defer_unopened_capture, user_id=user_id,
                attempt_id=attempt["attempt_id"], now=now, error=str(exc)[:1800])
            return {"status": "waiting_for_desktop", "reason": "Windows locked before Chrome opened; capture will catch up after unlock."}
        waiting = "[LOGIN_REQUIRED]" in str(exc)
        interrupted = desktop_interrupted(str(exc))
        await asyncio.to_thread(update_capture, user_id=user_id, attempt_id=attempt["attempt_id"],
            state="waiting_for_login" if waiting else "interrupted" if interrupted else "failed", error=str(exc)[:1800])
        if waiting:
            return {"status": "waiting_for_login", "reason": "Complete Google login in the open Chrome window, then retry; no report was imported."}
        if interrupted:
            return interrupted_summary()
        raise
    from argosy.services.research_worker import process_queue
    await record_agent_progress(agent="alpha_capture", state="completed",
        detail="Report imported; starting queued research reviews", correlation_id="alpha_capture")
    analysis = await process_queue(user_id=user_id)
    return {"status": "captured", "capture": result, "browser": browser,
            "research_queue": analysis, "failures": analysis.get("failures", [])}


class AlphaCaptureDailyJob(CadenceLoop):
    name = "alpha_capture_daily"

    def __init__(self, *, explicit_retry=False):
        self.explicit_retry = explicit_retry
        super().__init__(schedule=LoopSchedule(cron="0 18 * * *", timezone="Asia/Jerusalem"),
                         enabled=bool(configuration().get("enabled")))

    async def tick(self, *, now=None):
        return await run_daily(now=now() if now else None, explicit_retry=self.explicit_retry)


def alpha_capture_metadata():
    return JobMetadata(name=AlphaCaptureDailyJob.name, schedule_cron="0 18 * * *",
        schedule_human="Daily 18:00 Israel; catch up at sign-in/unlock",
        source_kind="ingest", description="Capture visible Meet Kevin Alpha posts using normal Chrome; catalog, deduplicate, and queue independent research. No automatic trades.")
