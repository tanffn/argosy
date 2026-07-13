from __future__ import annotations

import json

import pytest

from argosy.services.allocation_plan import (
    CASH_LABEL,
    SHORT_DURATION_IG_LABEL,
    build_target_allocation,
    normalize_override_labels,
)


CASH = CASH_LABEL
SHORT = SHORT_DURATION_IG_LABEL


def test_fold_short_duration_into_cash_adds_pct_and_drops_key() -> None:
    """Production path: durable overrides still carry Short-duration → must not 400."""
    out = normalize_override_labels({
        CASH: 6.95,
        SHORT: 2.98,
        "Strategic single-stock (NVDA)": 8.0,
    })
    assert SHORT not in out
    assert out[CASH] == pytest.approx(9.93)
    assert out["Strategic single-stock (NVDA)"] == 8.0


def test_fold_short_only_creates_cash_pin() -> None:
    out = normalize_override_labels({SHORT: 2.98})
    assert SHORT not in out
    assert out[CASH] == pytest.approx(2.98)


def test_fold_absent_short_is_noop_for_cash() -> None:
    out = normalize_override_labels({CASH: 6.95})
    assert out == {CASH: 6.95}


def test_engine_emits_ibta_under_cash_and_no_short_row() -> None:
    alloc = build_target_allocation()
    labels = [c.label for c in alloc.classes]
    assert SHORT not in labels
    cash = next(c for c in alloc.classes if c.label == CASH)
    syms = {i.symbol: i for i in cash.instruments}
    assert "IB01" in syms and syms["IB01"].role == "primary"
    assert syms["IB01"].weight_within_class_pct == pytest.approx(100.0)
    assert "IBTA" in syms and syms["IBTA"].role == "alt"
    assert syms["IBTA"].weight_within_class_pct == pytest.approx(0.0)
    assert alloc.cash_pct == pytest.approx(alloc.fi_pct, abs=0.02)


def test_engine_cash_equals_prior_cash_plus_short_under_pinned_fi_split() -> None:
    """Gate B: with Cash+Short pinned as on v91-style overrides, fold → Cash≈9.93."""
    pinned = normalize_override_labels({CASH: 6.95, SHORT: 2.98})
    assert pinned[CASH] == pytest.approx(9.93)
    alloc = build_target_allocation(authored_overrides=pinned)
    cash = next(c for c in alloc.classes if c.label == CASH)
    assert cash.target_pct == pytest.approx(9.93, abs=0.02)
    assert SHORT not in {c.label for c in alloc.classes}


def test_create_refinement_draft_rederives_ibta_under_cash(client_with_db) -> None:
    """Gate A: draft doc re-derived from engine — IBTA under Cash, no Short row."""
    import unittest.mock as mock

    from argosy.services.plan_refinement import create_refinement_draft
    from argosy.state.models import PlanVersion

    session_factory = client_with_db.app.state.session_factory
    with session_factory() as session:
        session.add(
            PlanVersion(
                user_id="ariel",
                role="current",
                version_label="gate-a-base",
                source_path="",
                raw_markdown="",
                target_allocation_overrides_json=json.dumps(
                    {
                        CASH: 6.95,
                        SHORT: 2.98,
                    }
                ),
            )
        )
        session.commit()

    fake_comp = {
        CASH: 10.0,
        "US broad-market core": 40.0,
        "Strategic single-stock (NVDA)": 8.0,
        "Dividend-quality income": 12.0,
        "Global quality growth (ex-NVDA-dense)": 8.0,
        "International developed (ex-US)": 10.0,
        "Emerging markets": 4.0,
        "US low-volatility equity": 4.0,
        "Real assets (REIT/TIPS)": 4.0,
    }

    with mock.patch(
        "argosy.services.target_allocation_doc.load_full_book_today_composition",
        return_value=fake_comp,
    ), mock.patch(
        "argosy.services.target_allocation_doc._prior_glide_q0",
        return_value=None,
    ):
        with session_factory() as session:
            draft = create_refinement_draft(session, "ariel", {})

    assert draft.target_allocation_json
    doc = json.loads(draft.target_allocation_json)
    labels = [c["label"] for c in doc["classes"]]
    assert SHORT not in labels
    cash = next(c for c in doc["classes"] if c["label"] == CASH)
    assert any(i["symbol"] == "IBTA" for i in cash["instruments"])
    assert cash["target_pct"] == pytest.approx(9.93, abs=0.05)
    merged = json.loads(draft.target_allocation_overrides_json)
    assert SHORT not in merged
    assert merged.get(CASH) == pytest.approx(9.93)
