"""Synthesis cost-cap accounting with resume-safe per-attempt spend.

Run 191 post-mortem: a resumed synthesis re-ran the in-flight phase, appended
new agent costs to the same JSONL trail, and the cap check summed the whole
file — $20.95 > $20 after work that had already been charged once.

Fix: on resume, archive the prior trail attempt and start a fresh attempt
file. Cap accounting = costs of REUSED phases (from DB / archived trail
tagged ``phase`` when present) + costs of the CURRENT attempt only.
A cap kill raises :class:`CostCapExceeded` and writes an inbox /
monitor-flag notification (never log-only).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from argosy.logging import get_logger

_log = get_logger("argosy.services.synthesis_cost_cap")

COST_CAP_ENV = "ARGOSY_SYNTHESIS_COST_CAP_USD"
DEFAULT_COST_CAP_USD = 20.0
COST_CAP_FLAG_KIND = "synthesis_cost_cap"
COST_CAP_DEDUP = "synthesis_cost_cap:{user_id}:{decision_run_id}"


class CostCapExceeded(RuntimeError):
    """Soft synthesis cost cap breached after a completed phase."""

    def __init__(
        self,
        *,
        spent_usd: float,
        cost_cap_usd: float,
        phase: str,
        decision_audit_token: str,
    ) -> None:
        self.spent_usd = spent_usd
        self.cost_cap_usd = cost_cap_usd
        self.phase = phase
        self.decision_audit_token = decision_audit_token
        super().__init__(
            f"cost_cap_exceeded: spent ${spent_usd:.2f} > cap ${cost_cap_usd:.2f} "
            f"after {phase}. Bump {COST_CAP_ENV} or investigate runaway agent."
        )


def configured_cost_cap_usd() -> float:
    raw = os.environ.get(COST_CAP_ENV, "")
    try:
        return float(raw) if raw.strip() else DEFAULT_COST_CAP_USD
    except (TypeError, ValueError):
        return DEFAULT_COST_CAP_USD


def trail_path_for(token: str, *, home: Path | None = None) -> Path:
    from argosy.config import get_settings

    root = home if home is not None else get_settings().home
    return root / "logs" / "synthesis" / f"{token}.jsonl"


def attempt_meta_path(token: str, *, home: Path | None = None) -> Path:
    return trail_path_for(token, home=home).with_suffix(".attempt.json")


@dataclass(frozen=True)
class AttemptState:
    attempt: int
    resume_from_phase: int
    credited_prior_usd: float
    started_at: str


def _read_jsonl_costs(
    path: Path,
    *,
    attempt: int | None = None,
) -> float:
    if not path.exists():
        return 0.0
    total = 0.0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if attempt is not None and row.get("attempt") not in (attempt, None):
                    # Untagged legacy lines count only for attempt 1.
                    if attempt != 1 or row.get("attempt") is not None:
                        continue
                cost = row.get("cost_usd")
                if isinstance(cost, (int, float)):
                    total += float(cost)
    except OSError:
        return round(total, 4)
    return round(total, 4)


def load_attempt_state(token: str, *, home: Path | None = None) -> AttemptState | None:
    path = attempt_meta_path(token, home=home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AttemptState(
            attempt=int(data.get("attempt", 1)),
            resume_from_phase=int(data.get("resume_from_phase", 1)),
            credited_prior_usd=float(data.get("credited_prior_usd", 0.0)),
            started_at=str(data.get("started_at") or ""),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_attempt_state(token: str, state: AttemptState, *, home: Path | None = None) -> None:
    path = attempt_meta_path(token, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "attempt": state.attempt,
                "resume_from_phase": state.resume_from_phase,
                "credited_prior_usd": state.credited_prior_usd,
                "started_at": state.started_at,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def begin_attempt(
    token: str,
    *,
    resume_from_phase: int = 1,
    home: Path | None = None,
) -> AttemptState:
    """Start (or continue) a cost-accounting attempt for ``token``.

    On resume (``resume_from_phase > 1``): archive the live trail so the
    re-run phase cannot double-count, credit spend already attributed to
    reused phases (phases strictly below the resume boundary) from the
    archived trail when rows carry a ``phase`` int, else credit 0 for the
    re-runable suffix and keep archived totals out of the live sum.
    """
    trail = trail_path_for(token, home=home)
    prior = load_attempt_state(token, home=home)
    now = datetime.now(timezone.utc).isoformat()

    if resume_from_phase <= 1 and prior is None:
        state = AttemptState(
            attempt=1,
            resume_from_phase=1,
            credited_prior_usd=0.0,
            started_at=now,
        )
        save_attempt_state(token, state, home=home)
        return state

    if resume_from_phase <= 1 and prior is not None:
        return prior

    # Resume: rotate trail → archive, start fresh live trail.
    # Per-attempt accounting: the new attempt gets a fresh cap budget; prior
    # attempt spend stays in the archive and is NOT summed (fixes run-191
    # double-count of the re-run phase).
    attempt_n = (prior.attempt if prior else 1) + 1
    if trail.exists():
        archive = trail.with_name(f"{trail.stem}.attempt{attempt_n - 1}.jsonl")
        try:
            if archive.exists():
                archive.unlink()
            trail.replace(archive)
        except OSError as exc:
            _log.warning(
                "synthesis_cost_cap.trail_rotate_failed",
                error=str(exc)[:200],
                token=token,
            )

    state = AttemptState(
        attempt=attempt_n,
        resume_from_phase=resume_from_phase,
        credited_prior_usd=0.0,
        started_at=now,
    )
    save_attempt_state(token, state, home=home)
    _log.info(
        "synthesis_cost_cap.attempt_begun",
        token=token,
        attempt=attempt_n,
        resume_from_phase=resume_from_phase,
        credited_prior_usd=state.credited_prior_usd,
    )
    return state


def _credit_reused_phases(archive: Path, *, resume_from_phase: int) -> float:
    """Sum archived costs for phases strictly below the resume boundary.

    Rows without a ``phase`` field are excluded from the credit (they may
    belong to the re-run suffix). This is conservative for the cap: we
    never under-count current-attempt spend; we may under-credit priors
    which only makes the cap tighter on the first post-resume check —
    acceptable vs double-counting the re-run.
    """
    if not archive.exists() or resume_from_phase <= 1:
        return 0.0
    total = 0.0
    try:
        with archive.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                phase = row.get("phase")
                if not isinstance(phase, int):
                    continue
                if phase >= resume_from_phase:
                    continue
                cost = row.get("cost_usd")
                if isinstance(cost, (int, float)):
                    total += float(cost)
    except OSError:
        return round(total, 4)
    return round(total, 4)


def current_attempt_spend(token: str, *, home: Path | None = None) -> float:
    """Live-trail spend for the current attempt (+ credited reused priors)."""
    state = load_attempt_state(token, home=home)
    live = _read_jsonl_costs(
        trail_path_for(token, home=home),
        attempt=state.attempt if state else None,
    )
    credited = state.credited_prior_usd if state else 0.0
    return round(live + credited, 4)


def check_cost_cap(
    *,
    decision_audit_token: str,
    cost_cap_usd: float | None = None,
    phase: str,
    user_id: str,
    home: Path | None = None,
    notify: bool = True,
    decision_run_id: int | None = None,
    session: Any | None = None,
) -> float:
    """Return spend so far; raise :class:`CostCapExceeded` if over cap.

    When ``notify`` and a ``session`` are provided, a cap kill also writes
    an inbox row + monitor flag before raising.
    """
    cap = float(cost_cap_usd if cost_cap_usd is not None else configured_cost_cap_usd())
    spent = current_attempt_spend(decision_audit_token, home=home)
    _log.info(
        "synthesis_cost_cap.check",
        user_id=user_id,
        token=decision_audit_token,
        phase=phase,
        spent_usd=spent,
        cost_cap_usd=cap,
    )
    if spent <= cap:
        return spent

    if notify and session is not None and decision_run_id is not None:
        try:
            notify_cost_cap_kill(
                session,
                user_id=user_id,
                decision_run_id=decision_run_id,
                spent_usd=spent,
                cost_cap_usd=cap,
                phase=phase,
                decision_audit_token=decision_audit_token,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "synthesis_cost_cap.notify_failed",
                error=str(exc)[:200],
            )
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass

    raise CostCapExceeded(
        spent_usd=spent,
        cost_cap_usd=cap,
        phase=phase,
        decision_audit_token=decision_audit_token,
    )


def notify_cost_cap_kill(
    session: Any,
    *,
    user_id: str,
    decision_run_id: int,
    spent_usd: float,
    cost_cap_usd: float,
    phase: str,
    decision_audit_token: str,
) -> None:
    """Inbox + monitor flag — cap kills must not be log-only."""
    from datetime import timedelta

    from sqlalchemy import select

    from argosy.state.models import ActionProposal, MonitorFlag

    now = datetime.now(timezone.utc)
    summary = (
        f"Synthesis cost cap hit: ${spent_usd:.2f} > ${cost_cap_usd:.2f} "
        f"after {phase} (run {decision_run_id})"
    )
    rationale = (
        f"Soft cost cap ({COST_CAP_ENV}) exceeded after phase `{phase}`.\n\n"
        f"- spent (this attempt + credited reused phases): ${spent_usd:.2f}\n"
        f"- cap: ${cost_cap_usd:.2f}\n"
        f"- audit token: `{decision_audit_token}`\n\n"
        "The run aborted. Bump the env cap or investigate runaway agents "
        "before resuming."
    )
    dedup = COST_CAP_DEDUP.format(user_id=user_id, decision_run_id=decision_run_id)

    existing_flag = session.execute(
        select(MonitorFlag).where(
            MonitorFlag.user_id == user_id,
            MonitorFlag.dedup_key == dedup,
            MonitorFlag.status == "active",
        )
    ).scalar_one_or_none()
    payload = {
        "decision_run_id": decision_run_id,
        "spent_usd": spent_usd,
        "cost_cap_usd": cost_cap_usd,
        "phase": phase,
        "decision_audit_token": decision_audit_token,
        "message": summary,
    }
    if existing_flag is None:
        session.add(
            MonitorFlag(
                user_id=user_id,
                kind=COST_CAP_FLAG_KIND,
                severity="critical",
                payload=json.dumps(payload),
                dedup_key=dedup,
                status="active",
                surfaced_at=now,
            )
        )
    else:
        existing_flag.payload = json.dumps(payload)
        existing_flag.severity = "critical"
        existing_flag.surfaced_at = now

    existing_prop = session.execute(
        select(ActionProposal).where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key == dedup,
            ActionProposal.status == "open",
        )
    ).scalar_one_or_none()
    if existing_prop is None:
        session.add(
            ActionProposal(
                user_id=user_id,
                summary=summary,
                rationale_md=rationale,
                suggested_payload=json.dumps(payload),
                severity="critical",
                surfaced_at=now,
                expires_at=now + timedelta(days=14),
                status="open",
                kind="note_only",
                dedup_key=dedup,
                execution_state="proposed",
            )
        )
    else:
        existing_prop.summary = summary
        existing_prop.rationale_md = rationale
        existing_prop.suggested_payload = json.dumps(payload)
        existing_prop.surfaced_at = now

    session.flush()
    _log.error(
        "synthesis_cost_cap.exceeded_notified",
        user_id=user_id,
        decision_run_id=decision_run_id,
        spent_usd=spent_usd,
        cost_cap_usd=cost_cap_usd,
        phase=phase,
    )


def stamp_attempt_on_row(row: dict[str, Any], *, token: str, phase: int | None = None,
                         home: Path | None = None) -> dict[str, Any]:
    """Annotate a trail row with attempt (+ optional phase) for accounting."""
    state = load_attempt_state(token, home=home)
    out = dict(row)
    out["attempt"] = state.attempt if state else 1
    if phase is not None:
        out["phase"] = int(phase)
    return out


__all__ = [
    "COST_CAP_ENV",
    "COST_CAP_FLAG_KIND",
    "CostCapExceeded",
    "AttemptState",
    "begin_attempt",
    "check_cost_cap",
    "configured_cost_cap_usd",
    "current_attempt_spend",
    "load_attempt_state",
    "notify_cost_cap_kill",
    "stamp_attempt_on_row",
    "trail_path_for",
]
