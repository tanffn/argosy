"""Aggregation helpers for the /expenses dashboard endpoints.

Two endpoints share these helpers:

    GET /api/expenses/dashboard-overview  → "year-at-a-glance" tab
    GET /api/expenses/dashboard-monthly   → per-month detail tab

All helpers are sync, take a SQLAlchemy `Session`, and never call an LLM.
They return Pydantic models from `argosy.api.routes.expenses` (the route
module currently owns the schema; importing back from there is fine for
the v0 of this extraction).
"""
from __future__ import annotations
