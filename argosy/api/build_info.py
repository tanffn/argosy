"""Build info captured at process start.

`GIT_SHA` is the short hash of the current HEAD at the moment the FastAPI
process imported this module. `STARTED_AT` is the corresponding UTC
timestamp. `/api/health` surfaces both so the UI can show whether the
running process matches the user's latest commit.

If `git` isn't available (e.g. detached environment, no `.git` dir),
`GIT_SHA` falls back to "unknown" — the health endpoint stays up regardless.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from argosy import __version__


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


GIT_SHA: str = _capture_git_sha()
STARTED_AT: datetime = datetime.now(timezone.utc)
VERSION: str = __version__


__all__ = ["GIT_SHA", "STARTED_AT", "VERSION"]
