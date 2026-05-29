"""Argosy operational scripts (CLI-invokable, not part of the API surface).

Modules here are executed via ``python -m argosy.scripts.<name>`` and
encapsulate one-off / ops-cadenced jobs: backfill verifications,
schema migrations, audits, etc. They are NOT imported by the API
runtime; importing one should be cheap (no DB connection at module
import).
"""
