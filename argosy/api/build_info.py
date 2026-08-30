"""Build info captured at process start.

`GIT_SHA` is the short hash of the current HEAD at the moment the FastAPI
process imported this module. `DECISION_CONTRACT_SHA` hashes the actual bytes
of the critical decision-path modules, including uncommitted changes.
`STARTED_AT` is the corresponding UTC timestamp. `/api/health` surfaces all
three so the UI can prove which source the running scheduler loaded.

If `git` isn't available (e.g. detached environment, no `.git` dir),
`GIT_SHA` falls back to "unknown" — the health endpoint stays up regardless.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from argosy import __version__

_DECISION_CONTRACT_FILES = (
    "argosy/agents/deployment_author.py",
    "argosy/agents/deployment_reviewer.py",
    "argosy/agents/stock_decision.py",
    "argosy/services/allocation_author/flow.py",
    "argosy/services/allocation_author/packet_assembly.py",
    "argosy/services/allocation_author/proposal.py",
    "argosy/services/allocation_author/reliable.py",
    "argosy/services/allocation_author/verifier.py",
    "argosy/services/current_recommendations.py",
    "argosy/services/deploy_decision_team.py",
    "argosy/services/sale_tax_facts.py",
    "argosy/services/staged_sell_policy.py",
    "argosy/services/sec_earnings_monitor.py",
    "argosy/services/order_sheet.py",
    "argosy/services/order_sheet_builder.py",
    "argosy/services/order_sheet_facts.py",
    "argosy/services/tax_simulation_ingest.py",
    "argosy/services/stock_decision/fetchers.py",
    "argosy/services/stock_decision/service.py",
)


def _capture_git_sha() -> str:
    """Return the current git short SHA, or 'unknown' if unavailable."""
    try:
        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _capture_decision_contract_sha() -> str:
    """Hash the decision source actually present when this process starts."""
    try:
        repo_root = Path(__file__).resolve().parents[2]
        digest = sha256()
        for relative_path in _DECISION_CONTRACT_FILES:
            path = repo_root / relative_path
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()[:12]
    except Exception:  # noqa: BLE001 - health must survive partial deployments
        return "unknown"


GIT_SHA: str = _capture_git_sha()
DECISION_CONTRACT_SHA: str = _capture_decision_contract_sha()
STARTED_AT: datetime = datetime.now(UTC)
VERSION: str = __version__


__all__ = ["DECISION_CONTRACT_SHA", "GIT_SHA", "STARTED_AT", "VERSION"]
