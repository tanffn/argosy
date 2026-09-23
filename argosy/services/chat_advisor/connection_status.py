"""Safe homepage projection of the private Discord worker's live state."""
from __future__ import annotations

from typing import Any


def connection_status(app_state: Any, *, user_id: str) -> dict:
    from argosy.transport.discord_advisor.config import load_config

    def result(status: str, message: str, attention: bool = False) -> dict:
        return {"status": status, "message": message, "attention_required": attention}

    startup_error = getattr(app_state, "discord_advisor_startup_error", None)
    try:
        config = load_config()
    except Exception:
        return result("configuration_error", "Local Discord configuration is invalid. Run the local setup command and restart Argosy.", True)
    if not config.enabled:
        return result("unconfigured", "Not enabled. Private server setup is pending; this is not a system failure.")
    if not any(binding.household_user_id == user_id for binding in config.bindings):
        return result("unconfigured", "No private Discord identity is configured for this household.")
    if startup_error:
        return result("startup_error", str(startup_error), True)
    job = getattr(app_state, "discord_advisor_job", None)
    if job is None:
        return result("stopped", "Configured, but the advisor worker is not running. Restart Argosy to load the configuration.", True)
    snapshot = job.status_snapshot()
    if snapshot.get("error"):
        from argosy.services.chat_advisor.outbound import OutboundFilter

        message = f"{snapshot['error']} {snapshot.get('repair') or 'Check the private Discord setup and restart Argosy.'}"
        return result("connection_error", OutboundFilter().redact(message), True)
    connection = snapshot.get("connection", "stopped")
    if connection == "connected":
        degraded = [
            str(message)
            for message in (
                snapshot.get("notification_error"),
                snapshot.get("status_board_error"),
            )
            if message
        ]
        if degraded:
            return result("connected", " ".join(degraded), True)
        return result("connected", "Connected to your private channel. Read-only advice and analysis requests are available while this PC is awake.")
    if connection == "reconnecting":
        return result("reconnecting", "Connecting or recovering the Discord connection. Replies may be delayed.")
    return result("stopped", "The private advisor is stopped. Check Job history or restart Argosy.", True)
