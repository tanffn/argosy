"""Readable final analysis receipts, with no new financial judgment."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _detail(value: Any, limit: int = 700) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "Not recorded."
    if isinstance(value, Mapping):
        text = "; ".join(f"{key.replace('_', ' ')}: {_detail(item, limit)}" for key, item in value.items())
    elif isinstance(value, Sequence) and not isinstance(value, str):
        text = "; ".join(_detail(item, limit) for item in value)
    else:
        text = str(value)
    return text if len(text) <= limit else text[:limit].rstrip() + "… (saved record has the full detail)"


def render_analysis_result(request_id: str, state: str, result: Any, error: str | None = None) -> str:
    """Render only persisted fleet facts; never turn a failure into consensus."""
    lines = [] if state == "completed" else [f"Review {state}."]
    outcomes = result.get("outcomes", []) if isinstance(result, Mapping) else []
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        ticker = outcome.get("ticker") or "Instrument"
        reused = outcome.get("blocked_by") == "verdict_defended"
        action = outcome.get("action") or outcome.get("verdict") or "No new actionable verdict"
        lines.extend(["", f"{ticker}: {action}" + (" — existing standing verdict reused" if reused else "")])
        assessment = outcome.get("news_assessment")
        if isinstance(assessment, Mapping):
            lines.append("News impact: " + _detail(assessment.get("summary") or assessment.get("rationale"), 320))
            if outcome.get("status") in {"error", "quorum_failed"}:
                lines.append("Incomplete review: " + _detail(outcome.get("blocked_reason"), 250))
            # The impact summary already explains why; avoid repeating the full rationale.
            if outcome.get("next_validation"):
                lines.append("Next review: " + str(outcome["next_validation"]))
            for url in (assessment.get("evidence_urls") or [])[:2]:
                if str(url).startswith("https://"):
                    lines.append(f"[News source]({url})")
            continue  # full falsifiers/sizing/catalysts stay available through the saved records
        if outcome.get("status") in {"error", "quorum_failed"}:
            lines.append("Incomplete review; this is not a fleet consensus.")
        if outcome.get("blocked_reason"):
            lines.append("Review status: " + _detail(outcome["blocked_reason"]))
        if outcome.get("rationale"):
            lines.append("Why: " + _detail(outcome["rationale"], 320))
        if outcome.get("confidence"):
            lines.append("Recorded confidence: " + _detail(outcome["confidence"]))
        if outcome.get("size"):
            lines.append("Recorded proposed size: " + _detail(outcome["size"]) + " (not an approved order)")
        if outcome.get("falsifiers"):
            lines.append("What would change this: " + _detail(outcome["falsifiers"], 180))
        if outcome.get("catalysts"):
            lines.append("Next catalyst: " + _detail(outcome["catalysts"], 180))
        if outcome.get("risk"):
            lines.append("Recorded risk controls: " + _detail(outcome["risk"]))
        if outcome.get("evaluation_due_at"):
            lines.append("Evaluation due: " + str(outcome["evaluation_due_at"]))
    if error:
        lines.append("Failure: " + _detail(error))
    lines.append("\nAnalysis only: no trade approved or executed.")
    return "\n".join(lines).strip()
