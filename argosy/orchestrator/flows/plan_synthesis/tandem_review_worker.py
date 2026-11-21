"""Child process for one codex-tandem review. Private JSON request/response on stdio."""

import contextlib
import json
from pathlib import Path
import sys


def run(scripts_dir, *, node_dir, prompt, role, decision_run_id, timeout_s, extra_args=None):
    sys.path.insert(0, str(scripts_dir))
    from engine_codex import run_codex

    return run_codex(
        node_dir=Path(node_dir), prompt=prompt, agent_name=f"{role}_run_{decision_run_id}",
        role=role, timeout_s=timeout_s, sandbox="read-only", extra_args=extra_args,
    )


if __name__ == "__main__":
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = run(**request)
        response = {"result": vars(result)}
    except ImportError:
        response = {"error": "installation"}
    except FileNotFoundError:
        response = {"error": "installation"}
    except Exception as exc:
        response = {"error": "installation" if "codex binary not resolvable" in str(exc).lower()
                    else type(exc).__name__}
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=True).encode("ascii"))
