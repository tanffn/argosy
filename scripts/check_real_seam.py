"""Real-seam coverage check.

Why this exists (2026-08-16): three incidents in one day, same shape — a
path was verified only through a fully-mocked LLM/DB seam (or by calling a
derivation helper directly instead of going through its real resolver), was
reported as working, and broke on its first real invocation:

  1. fund-vehicle verdict path: 31 passing tests, every one patched the LLM
     seam. First live call failed on ``report_obj.structured_output`` vs the
     real ``.output`` field. The path had never really run.
  2. gate-outcome persistence: a "verification receipt" reported as working
     twice — no live run had ever written a row, because the persist call
     sat in a code region the FM-rejected path never reached.
  3. fact-tokenizer headline numbers came from calling a derivation helper
     directly, bypassing a resolver bug that (it turned out) did not exist —
     the real path produced very different numbers.

This script makes "this path's only coverage is a mocked seam" VISIBLE
instead of silently passing. It is a REPORTING tool, not a silent gate: it
flags modules that look like agent-dispatch or DB-write paths and have zero
test carrying the ``@pytest.mark.real_seam`` marker (registered in
pyproject.toml) — with an explicit, reviewable allowlist
(``scripts/real_seam_allowlist.txt``) for modules that are deliberately
exempt (documented with a reason, not silently skipped).

Design choice — reporting script, not a pytest collector or blocking CI gate
(justification):
  - A pytest marker enforcement would need to run the FULL suite to see which
    modules are exercised by which tests; that's slow and duplicates work
    the suite already does. A static, standalone script is fast (~1s) and
    can run in a pre-commit hook, CI step, or ad hoc.
  - Failing CI outright on every gap would immediately break the build on
    ~200 legacy modules that predate this convention (see the --all output
    below) — the "fails constantly on legacy code gets disabled" trap named
    in this task. So: scoped to changed/untracked files by DEFAULT (where a
    fresh gap is actionable and cheap to fix), with an explicit --all mode
    for full-repo visibility (a known-gaps inventory, not a blocking gate).
  - It is deliberately NOT semantic (it cannot verify a `real_seam` test
    exercises the SPECIFIC bug-shaped seam — e.g. the innermost LLM call vs
    the whole dispatch function). That judgment is a code-review discipline;
    this script's job is only to make "zero real coverage at all" impossible
    to miss.

Usage:
    .venv/Scripts/python.exe scripts/check_real_seam.py               # changed/untracked files vs HEAD (default scope, exits 1 on any gap)
    .venv/Scripts/python.exe scripts/check_real_seam.py --all         # whole argosy/ tree (report only, always exits 0)
    .venv/Scripts/python.exe scripts/check_real_seam.py --file PATH   # check one specific file
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARGOSY_DIR = REPO_ROOT / "argosy"
TESTS_DIR = REPO_ROOT / "tests"
ALLOWLIST_PATH = REPO_ROOT / "scripts" / "real_seam_allowlist.txt"

# ---------------------------------------------------------------------------
# Risk heuristic: does this module look like an agent-dispatch or DB-write path?
# ---------------------------------------------------------------------------

_DISPATCH_PATTERNS = [
    re.compile(r"\bagent\.run\("),
    re.compile(r"_agent_factory\("),
    re.compile(r"\bawait\s+\w*[Aa]gent\w*\(.*\)\.run\("),
    re.compile(r"class\s+\w+\(BaseAgent"),
]
_DB_WRITE_PATTERNS = [
    re.compile(r"session\.add\("),
    re.compile(r"session\.commit\("),
    re.compile(r"\.execute\(\s*(delete|insert|update)\("),
    re.compile(r"session\.execute\(\s*sa\.text\(\s*[\"']INSERT"),
]

_EXCLUDE_DIR_PARTS = {"__pycache__", "migrations", "versions"}


def _risk_reason(text: str) -> str | None:
    if any(p.search(text) for p in _DISPATCH_PATTERNS):
        return "agent-dispatch"
    if any(p.search(text) for p in _DB_WRITE_PATTERNS):
        return "db-write"
    return None


def _dotted_module(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _load_allowlist() -> dict[str, str]:
    """dotted-module-path -> reason. Blank lines / '#' comments ignored."""
    out: dict[str, str] = {}
    if not ALLOWLIST_PATH.exists():
        return out
    for line in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            mod, reason = line.split("#", 1)
        else:
            mod, reason = line, ""
        out[mod.strip()] = reason.strip()
    return out


def _changed_python_files() -> list[Path]:
    """Working-tree changed + staged + untracked .py files under argosy/."""
    out: set[Path] = set()
    for args in (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            res = subprocess.run(
                args, cwd=REPO_ROOT, capture_output=True, text=True, check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line.endswith(".py"):
                continue
            p = REPO_ROOT / line
            try:
                p.relative_to(ARGOSY_DIR)
            except ValueError:
                continue
            if p.exists():
                out.add(p)
    return sorted(out)


def _all_python_files() -> list[Path]:
    return sorted(
        p for p in ARGOSY_DIR.rglob("*.py")
        if not any(part in _EXCLUDE_DIR_PARTS for part in p.parts)
    )


def _test_files_covering(dotted: str) -> list[Path]:
    """Test files that import this module (by dotted path substring)."""
    if not TESTS_DIR.exists():
        return []
    covering = []
    needle_a = f"import {dotted}"
    needle_b = f"from {dotted} import"
    # Also match a "from argosy.x.y import Z" where dotted is a prefix of the
    # module (e.g. module argosy.services.gate_outcome_store, import line
    # "from argosy.services.gate_outcome_store import persist_gate_outcomes").
    for tf in TESTS_DIR.rglob("test_*.py"):
        try:
            text = tf.read_text(encoding="utf-8")
        except OSError:
            continue
        if needle_a in text or needle_b in text:
            covering.append(tf)
    return covering


def _has_real_seam_marker(test_file: Path) -> bool:
    try:
        text = test_file.read_text(encoding="utf-8")
    except OSError:
        return False
    return "pytest.mark.real_seam" in text


def check(files: list[Path]) -> tuple[list[dict], list[dict]]:
    """Returns (gaps, ok) — each a list of dicts with module/reason/tests."""
    gaps: list[dict] = []
    ok: list[dict] = []
    allowlist = _load_allowlist()

    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        reason = _risk_reason(text)
        if reason is None:
            continue

        dotted = _dotted_module(f)
        if dotted in allowlist:
            ok.append({
                "module": dotted, "risk": reason, "tests": [],
                "note": f"allowlisted: {allowlist[dotted]}",
            })
            continue

        covering = _test_files_covering(dotted)
        real_seam_tests = [t for t in covering if _has_real_seam_marker(t)]

        if real_seam_tests:
            ok.append({
                "module": dotted, "risk": reason,
                "tests": [str(t.relative_to(REPO_ROOT)) for t in real_seam_tests],
            })
        else:
            gaps.append({
                "module": dotted, "risk": reason,
                "covering_tests": [str(t.relative_to(REPO_ROOT)) for t in covering],
            })
    return gaps, ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="scan the whole argosy/ tree (report only)")
    parser.add_argument("--file", action="append", default=[], help="check one specific file (repeatable)")
    args = parser.parse_args()

    if args.file:
        files = [Path(f).resolve() for f in args.file]
        scope = "explicit --file list"
    elif args.all:
        files = _all_python_files()
        scope = "whole argosy/ tree"
    else:
        files = _changed_python_files()
        scope = "changed/untracked files vs HEAD"

    gaps, ok = check(files)

    print("=" * 78)
    print("REAL-SEAM COVERAGE CHECK")
    print("=" * 78)
    print(f"Scope        : {scope}")
    print(f"Files scanned: {len(files)}")
    print(f"Risk modules : {len(gaps) + len(ok)}  (agent-dispatch or DB-write pattern found)")
    print()

    if ok:
        print(f"COVERED ({len(ok)}):")
        for item in ok:
            note = item.get("note")
            tests = ", ".join(item["tests"]) if item["tests"] else ""
            suffix = f" — {note}" if note else (f" via {tests}" if tests else "")
            print(f"  [{item['risk']:14s}] {item['module']}{suffix}")
        print()

    if gaps:
        print(f"GAPS ({len(gaps)}) — no @pytest.mark.real_seam test found, not allowlisted:")
        for item in gaps:
            print(f"  [{item['risk']:14s}] {item['module']}")
            if item["covering_tests"]:
                print(f"      covering (mocked-only) tests: {', '.join(item['covering_tests'])}")
            else:
                print("      covering tests: NONE FOUND")
        print()
        print("To fix a gap: add a test that exercises the real DB engine and/or a")
        print("real agent object (only the innermost LLM call stubbed, not the whole")
        print("dispatch function), mark it @pytest.mark.real_seam. Or, if this module")
        print(f"is a genuine exception, add it to {ALLOWLIST_PATH.relative_to(REPO_ROOT)}")
        print("with a one-line reason.")
    else:
        print("No gaps in scope.")

    print()
    if not args.all and not args.file and gaps:
        print("EXIT 1 — gaps in changed-file scope block by convention (not enforced by CI yet).")
        return 1
    if args.all:
        print("EXIT 0 — --all is report-only (legacy inventory, not a gate).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
