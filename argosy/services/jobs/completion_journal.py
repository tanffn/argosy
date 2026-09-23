"""Write-ahead completion receipts, independent of the busy application DB.

Private sidecars live beside the *actual session's* SQLite database, never in
the configured production home when a test/tenant uses a different database.
This is a bookkeeping outbox, not a queue for rerunning jobs or executing trades.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from argosy.logging import get_logger

_log = get_logger("argosy.jobs.completion_journal")


def validate_receipt(receipt: dict) -> None:
    """Reject malformed durable data before any values reach SQL bindings."""
    required_text = {"idempotency_key", "job_name", "started_at", "finished_at", "status"}
    nullable_text = {"database", "error_message", "skip_reason", "output_summary"}
    if type(receipt) is not dict or set(receipt) != required_text | nullable_text | {"run_id"}:
        raise ValueError("invalid completion receipt fields")
    if type(receipt["run_id"]) is not int or not 0 < receipt["run_id"] < 2**63:
        raise ValueError("invalid completion run ID")
    if any(type(receipt[k]) is not str or not receipt[k] for k in required_text):
        raise ValueError("invalid completion identity/outcome text")
    if any(receipt[k] is not None and type(receipt[k]) is not str for k in nullable_text):
        raise ValueError("invalid completion nullable text")
    if receipt["output_summary"] is not None:
        def invalid_constant(value):
            raise ValueError(f"non-JSON numeric constant: {value}")
        summary = json.loads(receipt["output_summary"], parse_constant=invalid_constant)
        if not isinstance(summary, dict):
            raise ValueError("completion summary must be a JSON object")


class CompletionJournal:
    def __init__(self, url):
        self.database = None
        self.directory = None
        if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
            self.database = str(Path(url.database).resolve())
            self.directory = Path(self.database + ".job-completions")

    def path(self, receipt: dict) -> Path:
        assert self.directory is not None
        key = hashlib.sha256(receipt["idempotency_key"].encode()).hexdigest()
        return self.directory / f"{int(receipt['run_id'])}-{key}.json"

    def stage(self, receipt: dict) -> dict:
        if self.directory is None:  # In-memory test databases have no restart lifetime.
            return receipt
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path(receipt)
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            # An ambiguous commit retry must preserve the ORIGINAL finish time.
            if {k: v for k, v in existing.items() if k != "finished_at"} != {
                k: v for k, v in receipt.items() if k != "finished_at"
            }:
                raise ValueError(f"conflicting completion receipt for run {receipt['run_id']}")
            return existing
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, sort_keys=True, default=str)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Publish a complete file atomically without overwriting a
                # concurrent closer's receipt (NTFS and POSIX both support it).
                os.link(temporary, path)
            except FileExistsError:
                return self.stage(receipt)
        finally:
            temporary.unlink(missing_ok=True)
        return receipt

    def pending(self, *, strict=True):
        if self.directory is None or not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.json")):
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                validate_receipt(receipt)
                if receipt.get("database") != self.database or path != self.path(receipt):
                    raise ValueError(f"completion journal identity mismatch: {path.name}")
                yield receipt
            except (ValueError, TypeError, KeyError, AttributeError):
                if strict:
                    raise
                _log.exception("jobs.completion_receipt_invalid", receipt_file=path.name)

    def acknowledge(self, receipt: dict) -> None:
        if self.directory is not None:
            self.path(receipt).unlink(missing_ok=True)
