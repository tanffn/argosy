"""Systematic statement-format audit matrix.

Walks ``ARGOSY_EXPENSE_SAMPLES_ROOT`` and emits one row per file:
parseable statements get sniffed/parsed/oracle/sanity/identity columns;
non-statement artifacts are SKIPPED with an explicit reason (no silent
omissions). Shared by ``scripts/audit_statement_formats.py`` and the
samples-gated CI tripwire in ``tests/test_expense_format_audit.py``.

This is the format-drift tripwire: parser rows must equal oracle rows,
and |declared − parsed| must stay within the ingest-sanity tolerance.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal

from argosy.config import ExpensesIngestSanityConfig
from argosy.services.expense_ingest.parse_sanity import (
    ParseSanityError,
    check_parse_sanity,
)
from argosy.services.expense_ingest.sniff import UnknownFormatError, detect_format
from argosy.services.expense_ingest.types import ParserName, ParseResult


def _ensure_repo_root_on_path() -> Path:
    """``tests.expense_ground_truth`` lives at the repo root (not a package)."""
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    return repo

# Statement-ish extensions we always consider. Everything else under the
# samples root is still listed as SKIPPED so the matrix has no holes.
_STATEMENT_EXTS = {".xls", ".xlsx"}
_KNOWN_NON_STATEMENT_EXTS = {".tsv", ".csv", ".md", ".py", ".bak", ".txt"}

# Path / name markers for artifacts that are intentionally not expense
# statements (portfolio XLS, Schwab equity CSVs, household TSV dumps).
_SKIP_NAME_MARKERS = (
    "protfolio",  # Leumi typo in live filenames
    "portfolio",
    "equityawards",
    "family finances status",
    "buy_planner",
    "nvidia simulation",
    "portfolio_holdings",
)
_SKIP_DIR_MARKERS = (
    "schwab",
    "alpha report",
)

# Max rolling title: '...לכרטיס מאסטרקארד 6225'
_ROLLING_TITLE_LAST4 = re.compile(r"לכרטיס\s+\S+\s+(\d{4})\b")
_ROLLING_FNAME_LAST4 = re.compile(r"^(\d{4})_")
_FOLDER_LAST4 = re.compile(r"^\d{4}$")


IdentityKind = Literal[
    "content",           # last4/account read from file body / sheet
    "filename",          # derived from filename convention
    "folder",            # derived from parent folder (card number)
    "needs_hint",        # file does not self-identify; caller must supply
    "fallback_bank_acct",  # Max monthly sheet-name bank-account last-4 (wrong)
    "n/a",
]

StatusKind = Literal["OK", "FAIL", "SKIPPED", "ERROR"]


@dataclass
class AuditRow:
    rel_path: str
    status: StatusKind
    skip_reason: str = ""
    sniffed: str = ""
    rows_parsed: int | None = None
    rows_oracle: int | None = None
    declared_total: float | None = None
    parsed_total: float | None = None
    delta: float | None = None
    sanity: str = ""
    identity: IdentityKind = "n/a"
    external_id: str = ""
    notes: str = ""
    unconsumed_sheets: list[str] = field(default_factory=list)

    @property
    def is_mismatch(self) -> bool:
        if self.status in ("FAIL", "ERROR"):
            return True
        if self.status != "OK":
            return False
        if (
            self.rows_parsed is not None
            and self.rows_oracle is not None
            and self.rows_parsed != self.rows_oracle
        ):
            return True
        if self.delta is not None and self.declared_total is not None:
            tol = _total_tolerance(self.declared_total)
            if abs(self.delta) > tol:
                return True
        if self.unconsumed_sheets:
            return True
        if self.sanity.startswith("FAIL"):
            return True
        return False


def _total_tolerance(declared: float, cfg: ExpensesIngestSanityConfig | None = None) -> float:
    cfg = cfg or ExpensesIngestSanityConfig()
    return max(cfg.total_tolerance_nis, cfg.total_tolerance_pct / 100.0 * abs(declared))


def samples_root(env: dict[str, str] | None = None) -> Path | None:
    env = env or os.environ
    raw = env.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def classify_skip(root: Path, path: Path) -> str | None:
    """Return a skip reason, or None if the file should be audited as a statement."""
    name_l = path.name.lower()
    rel_l = _rel(root, path).lower()
    parts_l = [p.lower() for p in path.relative_to(root).parts]

    if any(m in parts_l for m in _SKIP_DIR_MARKERS):
        return f"non-statement dir ({next(m for m in _SKIP_DIR_MARKERS if m in parts_l)})"
    if any(m in name_l or m in rel_l for m in _SKIP_NAME_MARKERS):
        marker = next(m for m in _SKIP_NAME_MARKERS if m in name_l or m in rel_l)
        return f"non-statement artifact ({marker})"

    suf = path.suffix.lower()
    if suf in _KNOWN_NON_STATEMENT_EXTS or name_l.endswith(".tsv.bak_pre_fix") or ".bak" in name_l:
        return f"non-statement extension ({suf or 'bak'})"
    if suf not in _STATEMENT_EXTS:
        return f"non-statement extension ({suf or 'none'})"

    # Root-level / misc SpreadsheetML portfolio dumps (Leumi_26_May_01.xls,
    # Aug 25.xls) — XML wrapper, not the HTML Leumi bank statement.
    try:
        head = path.read_bytes()[:256].lstrip()
    except OSError as e:
        return f"unreadable ({e})"
    if head.startswith(b"<?xml") or b"mso-application" in head[:200]:
        return "portfolio SpreadsheetML (not HTML bank statement)"

    return None


def _folder_last4_hint(path: Path) -> str | None:
    """Card-number folder (…/6225/Apr.xlsx) → hint for parsers that need it."""
    parent = path.parent.name
    if _FOLDER_LAST4.match(parent):
        return parent
    # Also accept '<last4>_…' filename as a soft hint.
    m = _ROLLING_FNAME_LAST4.match(path.name)
    return m.group(1) if m else None


_ORACLE_MOD = None


def _load_oracle_module():
    """Load ``tests/expense_ground_truth.py`` once (cached)."""
    global _ORACLE_MOD
    if _ORACLE_MOD is not None:
        return _ORACLE_MOD
    import importlib.util

    repo = _ensure_repo_root_on_path()
    gt_path = repo / "tests" / "expense_ground_truth.py"
    # Prefer a normal package import when the repo root is on sys.path
    # (pytest always has this); fall back to file-path load for
    # ``python scripts/audit_statement_formats.py``.
    try:
        from tests import expense_ground_truth as mod  # type: ignore
        _ORACLE_MOD = mod
        return mod
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location(
        "argosy_expense_ground_truth", gt_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load ground-truth oracle from {gt_path}")
    mod = importlib.util.module_from_spec(spec)
    # Dataclasses inside the module need the module registered before exec.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _ORACLE_MOD = mod
    return mod


def _oracle_for(name: ParserName):
    mod = _load_oracle_module()
    return {
        ParserName.LEUMI_OSH: mod.leumi_oracle,
        ParserName.LEUMI_USD: mod.leumi_usd_oracle,
        ParserName.ISRACARD: mod.isracard_oracle,
        ParserName.MAX: mod.max_oracle,
        ParserName.DISCOUNT: mod.discount_oracle,
    }[name]


def _parser_for(name: ParserName) -> Callable:
    from argosy.services.expense_ingest.parsers import (
        discount as p_discount,
        isracard as p_isra,
        leumi_osh as p_leumi,
        leumi_usd as p_leumi_usd,
        max as p_max,
    )
    return {
        ParserName.LEUMI_OSH: p_leumi.parse,
        ParserName.LEUMI_USD: p_leumi_usd.parse,
        ParserName.ISRACARD: p_isra.parse,
        ParserName.MAX: p_max.parse,
        ParserName.DISCOUNT: p_discount.parse,
    }[name]


def _classify_identity(
    parser_name: ParserName,
    result: ParseResult,
    path: Path,
    *,
    hint_used: str | None,
) -> tuple[IdentityKind, str]:
    ext_id = (result.source_hint.external_id if result.source_hint else "") or ""
    if parser_name in (ParserName.LEUMI_OSH, ParserName.LEUMI_USD):
        return ("content" if ext_id else "needs_hint"), ext_id
    if parser_name == ParserName.ISRACARD:
        return ("content" if ext_id else "needs_hint"), ext_id
    if parser_name == ParserName.DISCOUNT:
        return ("content" if ext_id else "needs_hint"), ext_id
    if parser_name == ParserName.MAX:
        # Rolling: title > hint > filename. Monthly: hint required; sheet
        # name is the bank-account last-4 (wrong for card identity).
        if result.rolling:
            title_hit = False
            try:
                import pandas as pd
                df = pd.read_excel(
                    path, sheet_name="פירוט עסקאות וזיכויים", header=None,
                )
                title = str(df.iat[0, 0]) if not pd.isna(df.iat[0, 0]) else ""
                title_hit = bool(_ROLLING_TITLE_LAST4.search(title))
            except Exception:
                title_hit = False
            if title_hit and ext_id:
                return "content", ext_id
            if hint_used and ext_id == hint_used:
                return "folder", ext_id
            if _ROLLING_FNAME_LAST4.match(path.name):
                return "filename", ext_id
            return ("content" if ext_id else "needs_hint"), ext_id
        # Monthly Max-format: without hint the parser falls back to bank acct.
        if hint_used and ext_id == hint_used:
            return "folder", ext_id
        if ext_id:
            return "fallback_bank_acct", ext_id
        return "needs_hint", ext_id
    return "n/a", ext_id


def _unconsumed_discount_sheets(path: Path) -> list[str]:
    """Flag transaction-looking sheets the discount parser does not read."""
    from argosy.services.expense_ingest.parsers.discount import CONSUMED_SHEETS
    try:
        import pandas as pd
        xl = pd.ExcelFile(path)
    except Exception:
        return []
    return sorted(
        s for s in xl.sheet_names
        if s.startswith("עסקאות") and s not in CONSUMED_SHEETS
    )


def audit_file(root: Path, path: Path) -> AuditRow:
    rel = _rel(root, path)
    skip = classify_skip(root, path)
    if skip:
        return AuditRow(rel_path=rel, status="SKIPPED", skip_reason=skip)

    try:
        sniffed = detect_format(path)
    except UnknownFormatError as e:
        return AuditRow(
            rel_path=rel, status="FAIL", sniffed="?",
            notes=f"UnknownFormatError: {e}",
            sanity="n/a",
        )
    except Exception as e:  # noqa: BLE001
        from argosy.services.expense_ingest.parsers.leumi_html import (
            LeumiCustodyViewError,
        )
        if isinstance(e, LeumiCustodyViewError):
            return AuditRow(
                rel_path=rel, status="SKIPPED",
                skip_reason="securities custody view (rejected by design)",
            )
        return AuditRow(
            rel_path=rel, status="ERROR", sniffed="?",
            notes=f"sniff raised {type(e).__name__}: {e}",
            sanity="n/a",
        )

    if sniffed not in (
        ParserName.LEUMI_OSH, ParserName.LEUMI_USD, ParserName.ISRACARD,
        ParserName.MAX, ParserName.DISCOUNT,
    ):
        return AuditRow(
            rel_path=rel, status="FAIL", sniffed=sniffed.value,
            notes=f"no audit oracle/parser wired for {sniffed.value}",
            sanity="n/a",
        )

    hint = _folder_last4_hint(path)
    parse_fn = _parser_for(sniffed)
    try:
        if sniffed == ParserName.MAX:
            result = parse_fn(path, last4_hint=hint)
        else:
            result = parse_fn(path)
    except Exception as e:  # noqa: BLE001 — loud failure is the point
        from argosy.services.expense_ingest.parsers.leumi_html import (
            LeumiCustodyViewError,
        )
        if isinstance(e, LeumiCustodyViewError):
            # Rejected BY DESIGN: securities-custody sub-account view, not
            # a cash ledger — ingesting it double-counts booked trades.
            return AuditRow(
                rel_path=rel, status="SKIPPED",
                skip_reason="securities custody view (rejected by design)",
            )
        return AuditRow(
            rel_path=rel, status="FAIL", sniffed=sniffed.value,
            notes=f"parse raised {type(e).__name__}: {e}",
            sanity="n/a", identity="n/a",
        )

    try:
        truth = _oracle_for(sniffed)(path)
    except Exception as e:  # noqa: BLE001
        return AuditRow(
            rel_path=rel, status="FAIL", sniffed=sniffed.value,
            rows_parsed=len(result.transactions),
            notes=f"oracle raised {type(e).__name__}: {e}",
            sanity="n/a",
        )

    declared = result.statement.declared_total_nis
    parsed_total = float(result.statement.parsed_total_nis)
    delta = None if declared is None else parsed_total - float(declared)

    sanity = "PASS"
    try:
        report = check_parse_sanity(result)
        if report.warnings:
            sanity = f"PASS ({len(report.warnings)} warn)"
    except ParseSanityError as e:
        sanity = f"FAIL: {e.violations[0]}"

    identity, ext_id = _classify_identity(
        sniffed, result, path, hint_used=hint,
    )

    notes_parts: list[str] = []
    unconsumed: list[str] = []
    if sniffed == ParserName.DISCOUNT:
        unconsumed = _unconsumed_discount_sheets(path)
        if unconsumed:
            notes_parts.append("unconsumed sheets: " + ",".join(unconsumed))

    if len(result.transactions) != truth.row_count:
        notes_parts.append(
            f"row mismatch parser={len(result.transactions)} oracle={truth.row_count}"
        )
    if delta is not None and abs(delta) > _total_tolerance(declared or 0.0):
        notes_parts.append(
            f"total delta {delta:.2f} > tol {_total_tolerance(declared or 0.0):.2f}"
        )

    status: StatusKind = "OK"
    row = AuditRow(
        rel_path=rel,
        status=status,
        sniffed=sniffed.value,
        rows_parsed=len(result.transactions),
        rows_oracle=truth.row_count,
        declared_total=declared,
        parsed_total=parsed_total,
        delta=delta,
        sanity=sanity,
        identity=identity,
        external_id=ext_id,
        notes="; ".join(notes_parts),
        unconsumed_sheets=unconsumed,
    )
    if row.is_mismatch:
        row.status = "FAIL"
    return row


def iter_sample_files(root: Path) -> Iterable[Path]:
    """Every file under root (sorted) — skip decisions happen per-file."""
    files = [p for p in root.rglob("*") if p.is_file()]
    return sorted(files, key=lambda p: str(p).lower())


def run_audit(root: Path | None = None) -> list[AuditRow]:
    root = root or samples_root()
    if root is None:
        raise FileNotFoundError(
            "ARGOSY_EXPENSE_SAMPLES_ROOT unset or not a directory"
        )
    return [audit_file(root, p) for p in iter_sample_files(root)]


def format_matrix(rows: list[AuditRow]) -> str:
    """Human-readable fixed-width matrix for the hand-back / CLI."""
    headers = (
        "status", "sniffed", "rows_p", "rows_o", "declared", "parsed",
        "delta", "sanity", "identity", "ext_id", "file", "notes",
    )
    lines = ["\t".join(headers)]
    for r in rows:
        if r.status == "SKIPPED":
            lines.append("\t".join([
                r.status, "-", "-", "-", "-", "-", "-", "-", "-", "-",
                r.rel_path, r.skip_reason,
            ]))
            continue
        lines.append("\t".join([
            r.status,
            r.sniffed or "-",
            "-" if r.rows_parsed is None else str(r.rows_parsed),
            "-" if r.rows_oracle is None else str(r.rows_oracle),
            "-" if r.declared_total is None else f"{r.declared_total:.2f}",
            "-" if r.parsed_total is None else f"{r.parsed_total:.2f}",
            "-" if r.delta is None else f"{r.delta:.2f}",
            r.sanity or "-",
            r.identity,
            r.external_id or "-",
            r.rel_path,
            r.notes or r.skip_reason,
        ]))
    n_ok = sum(1 for r in rows if r.status == "OK")
    n_fail = sum(1 for r in rows if r.status in ("FAIL", "ERROR") or r.is_mismatch)
    n_skip = sum(1 for r in rows if r.status == "SKIPPED")
    lines.append(
        f"# summary: total={len(rows)} OK={n_ok} FAIL={n_fail} SKIPPED={n_skip}"
    )
    return "\n".join(lines) + "\n"


def mismatch_rows(rows: list[AuditRow]) -> list[AuditRow]:
    return [r for r in rows if r.is_mismatch or r.status in ("FAIL", "ERROR")]


__all__ = [
    "AuditRow",
    "audit_file",
    "format_matrix",
    "mismatch_rows",
    "run_audit",
    "samples_root",
]
