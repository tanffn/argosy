from argosy.agents.youtube_analysis import YouTubeSkepticAgent
from argosy.services.chat_advisor.conversation import GroundedAnswerAgent
from argosy.services.chat_advisor.daily_overview import BRIEFING_CONTRACT
from argosy.services.chat_advisor.read_team import REASONING_CONTRACT, AnswerCoverageAgent


def test_author_and_independent_reviewer_can_explain_without_new_trade_advice():
    author, _, _ = GroundedAnswerAgent(user_id="test").build_prompt(
        question="Does that explanation address my objection?", history="", topic="research",
        payload="{}", warnings="")
    reviewer, _ = AnswerCoverageAgent(user_id="test").build_prompt(
        question="Does that explanation address my objection?", history="", hints="",
        payload="{}", receipts="[]", draft="", catalog="[]")
    assert REASONING_CONTRACT in author and REASONING_CONTRACT in reviewer
    assert "next_offset in a short continuation note" not in author
    assert "never user-facing prose" in author


def test_source_skeptic_has_materiality_and_context_contract():
    prompt, _, _ = YouTubeSkepticAgent(user_id="test").build_prompt(transcript="example", source_id="source")
    assert "align the instrument, definition, timestamp and context" in prompt
    assert "not by itself evidence that a thesis is false" in prompt


def test_daily_screening_mandate_is_not_general_investment_rejection():
    assert "not a general SELL/avoid verdict" in BRIEFING_CONTRACT
    assert "not simply an offer to display a raw article excerpt" in BRIEFING_CONTRACT


def test_followup_offer_is_reviewed_against_actual_evidence_depth():
    from argosy.services.chat_advisor.daily_overview import (
        DailyBriefingAuthor,
        DailyBriefingReviewer,
    )

    assert "The reviewer must check" in BRIEFING_CONTRACT
    assert "immutable agent_report reference" in BRIEFING_CONTRACT
    author, _, _ = DailyBriefingAuthor(user_id="test").build_prompt(
        question="Daily brief", history="", topic="discovery", payload="{}", warnings="")
    reviewer, _ = DailyBriefingReviewer(user_id="test").build_prompt(
        question="Daily brief", history="", hints="", payload="{}", receipts="[]", draft="", catalog="[]")
    assert BRIEFING_CONTRACT in author and BRIEFING_CONTRACT in reviewer
    assert "correct any overpromise" in REASONING_CONTRACT
