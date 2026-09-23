from argosy.services.chat_advisor.presentation import render_analysis_result


def test_final_receipt_is_readable_and_does_not_invent_missing_financial_facts():
    text = render_analysis_result("req1", "completed", {"outcomes": [{
        "ticker": "TEST", "action": "HOLD", "rationale": "The catalyst is still unresolved.",
        "decision_run_id": 8, "verdict_id": 9,
    }]})
    assert "TEST: HOLD" in text
    assert "Why: The catalyst" in text
    assert "Not recorded" not in text
    assert "proposed size" not in text
    assert "decision run 8" not in text
    assert "no trade approved or executed" in text


def test_reused_and_failed_reviews_are_not_presented_as_fresh_consensus():
    text = render_analysis_result("req2", "failed", {"outcomes": [
        {"ticker": "OLD", "blocked_by": "verdict_defended", "status": "blocked"},
        {"ticker": "NEW", "status": "quorum_failed", "blocked_reason": "Only one analyst succeeded"},
    ]}, "Review incomplete")
    assert "existing standing verdict reused" in text
    assert "not a fleet consensus" in text
    assert "Review incomplete" in text
