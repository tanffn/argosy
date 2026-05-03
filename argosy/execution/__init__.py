"""Argosy execution layer (SDD §10, Phase 4).

Owns the path from APPROVED proposal to broker placement (or PaperFill
log) plus the reconcile loop that turns broker fills into `fills` rows
and advances proposals to `EXECUTED_LIVE`.

Submodules are imported lazily here to avoid circular-import pitfalls:
the IBKR adapter pulls in `execution.audit`, and the router pulls in
the IBKR adapter. Importing only the leaf modules below means
`from argosy.execution import audit` works even mid-bootstrap.
"""

# Re-export the leaf helpers without forcing the heavier router import,
# so packages that only need audit helpers (e.g., the IBKR adapter)
# can do `from argosy.execution.audit import ...` without circulars.

__all__: list[str] = [
    "ExecutionRouter",
    "ReconcileLoop",
    "record_audit_event",
    "write_paper_fill",
]


def __getattr__(name: str):
    """PEP 562: lazy module attribute access to break import cycles."""
    if name == "ExecutionRouter":
        from argosy.execution.router import ExecutionRouter

        return ExecutionRouter
    if name == "ReconcileLoop":
        from argosy.execution.reconcile import ReconcileLoop

        return ReconcileLoop
    if name == "record_audit_event":
        from argosy.execution.audit import record_audit_event

        return record_audit_event
    if name == "write_paper_fill":
        from argosy.execution.audit import write_paper_fill

        return write_paper_fill
    raise AttributeError(f"module 'argosy.execution' has no attribute {name!r}")
