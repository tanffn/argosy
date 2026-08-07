"""Fleet falsifier / revisit-trigger coercion — the storage-layer floor that
keeps malformed or degenerate fleet output out of the verdict registry.

Covers the three defects a Codex adversarial review flagged on the
falsifier-authoring change: (1) metric_condition without a valid op, (2)
non-fireable / non-finite trigger values, (3) generic market-move falsifiers
that would unlock a DEFENDED verdict on any bearish headline (PLTR-scar).
"""
from __future__ import annotations

import pytest

from argosy.agents.trader import RevisitTrigger, TraderProposal
from argosy.decisions.flow import (
    _coerce_verdict_falsifiers,
    _is_degenerate_falsifier,
)


@pytest.mark.parametrize(
    "generic",
    [
        "stock drops",
        "the market falls",
        "PLTR stock drops",
        "stock price falls",
        "shares sell off",
        "stock plunges",
        "it falls sharply",
        "shares pull back",
        "short",  # too short
    ],
)
def test_generic_falsifiers_are_degenerate(generic):
    assert _is_degenerate_falsifier(generic)


@pytest.mark.parametrize(
    "specific",
    [
        "TTM free cash flow turns sustainably positive with debt stable",
        "net revenue retention falls under 115%",
        "dividend cut announced",
        "key drug fails phase 3 trial",
        "stock drops below $80 on no fundamental news",  # has a numeric threshold
        "gross margin compresses two quarters running",
    ],
)
def test_specific_falsifiers_are_kept(specific):
    assert not _is_degenerate_falsifier(specific)


def test_blank_and_generic_falsifiers_dropped_in_coercion():
    class TP:
        falsifiers = ["stock plunges", "  ", "margin below 55% two quarters"]
        revisit_triggers: list = []

    fals, trigs = _coerce_verdict_falsifiers(TP())
    assert fals == ["margin below 55% two quarters"]
    assert trigs == []


def test_metric_condition_requires_valid_op():
    class TP:
        falsifiers = ["revenue growth decelerates below 20% for two quarters"]
        revisit_triggers = [
            {"kind": "metric_condition", "metric": "nrr", "op": "not_above", "value": 120},
            {"kind": "metric_condition", "metric": "gm", "value": 55},  # no op
            {"kind": "metric_condition", "metric": "gm", "op": "<", "value": 55},  # valid
        ]

    _, trigs = _coerce_verdict_falsifiers(TP())
    assert len(trigs) == 1
    assert trigs[0]["op"] == "<" and trigs[0]["value"] == 55.0


def test_non_finite_and_non_numeric_prices_dropped():
    class TP:
        falsifiers = ["free cash flow margin turns negative for a full year"]
        revisit_triggers = [
            {"kind": "price_below", "price": "NaN"},
            {"kind": "price_above", "price": "inf"},
            {"kind": "price_below", "price": "below 80"},
            {"kind": "price_below", "price": 115},  # valid
        ]

    _, trigs = _coerce_verdict_falsifiers(TP())
    assert [t["kind"] for t in trigs] == ["price_below"]
    assert trigs[0]["price"] == 115.0 and isinstance(trigs[0]["price"], float)


def test_bad_iso_date_dropped():
    class TP:
        falsifiers = ["clinical trial readout misses primary endpoint"]
        revisit_triggers = [
            {"kind": "dated_event", "date": "Q4 earnings"},
            {"kind": "dated_event", "date": "2026-10-31", "label": "Q3"},
        ]

    _, trigs = _coerce_verdict_falsifiers(TP())
    assert [t["date"] for t in trigs] == ["2026-10-31"]


def test_unknown_kind_dropped_before_write():
    class TP:
        falsifiers = ["operating margin falls below prior-year level for two quarters"]
        revisit_triggers = [{"kind": "garbage"}, {"kind": "price_above", "price": 500.0}]

    _, trigs = _coerce_verdict_falsifiers(TP())
    assert [t["kind"] for t in trigs] == ["price_above"]


def test_typed_pydantic_path_roundtrips():
    p = TraderProposal(
        ticker="ORCL",
        action="hold",
        size_shares_or_currency=0,
        rationale_summary="r",
        falsifiers=["TTM free cash flow turns sustainably positive with debt stable"],
        revisit_triggers=[
            RevisitTrigger(kind="price_below", price=115.0),
            RevisitTrigger(kind="dated_event", date="2026-10-31", label="Q3"),
        ],
    )
    fals, trigs = _coerce_verdict_falsifiers(p)
    assert len(fals) == 1
    assert [t["kind"] for t in trigs] == ["price_below", "dated_event"]


def test_new_fields_are_optional_backward_compat():
    p = TraderProposal(
        ticker="X", action="hold", size_shares_or_currency=0, rationale_summary="r"
    )
    assert p.falsifiers == [] and p.revisit_triggers == []
    assert "falsifiers" in TraderProposal.model_json_schema()["properties"]
