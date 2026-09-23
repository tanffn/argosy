"""Value-led daily briefing using the same read-only answer/review team as chat."""
from datetime import datetime

from .contracts import ChatAnswer, Principal
from .read_team import AnswerCoverageAgent, ReadExecutor, ReadRequest, answer_with_review


async def build_daily_overview(retrieval, principal: Principal, *, now: datetime,
                               answerer=None, reviewer=None) -> ChatAnswer:
    from .conversation import GroundedAnswerAgent

    executor = ReadExecutor(retrieval, principal)
    for topic, filters in (("news", {"limit": 12}), ("discovery", {"limit": 6}),
                           ("actions", {"record_id": "current_trade_plan"}), ("holdings", {})):
        await executor.read(ReadRequest(topic=topic, filters=filters))
    question = (
        f"Write my daily investment briefing as of {now.isoformat()}. "
        "At most three short bullets and 100 words total; fewer when appropriate. "
        "Select only useful news, its relevance to my actual holdings, a worthwhile discovery "
        "highlight, or a material change in the CURRENT actionable recommendation. "
        "Say what happened and why I should care. A discovery ranking is NOT a buy instruction. "
        "Do not invent allocations, advice, catalysts or a fresh fleet assessment. "
        "Distinguish publication, ingestion, and review dates. Do not present old research as new. "
        "A model-authored future catalyst date is not verified: only call it scheduled/confirmed "
        "if dated external evidence supports it; otherwise attribute the rationale to the saved review. "
        "Only the current trade plan establishes today's proposed trades; do not turn older "
        "critique notes or historical plan prose into a new request for action. "
        "Ignore queue counts, empty trade plans, routine paperwork and system-status boilerplate. "
        "If the checked evidence has no worthwhile update, simply say: "
        "'Nothing worth highlighting in the latest reviewed news or discovery today.' "
        "If sources failed or are stale, state the coverage limitation briefly instead of "
        "claiming nothing happened. This is a bounded saved-research review, not a live market scan. "
        "Lead with the useful points, not methodology. Express any necessary coverage caveat "
        "in plain language in one short line (e.g. 'Saved research; news dates unverified.'). "
        "Do not expose extraction, excerpting, pagination, queue counts, or other internal mechanics. "
        "No greeting, sign-off, source IDs, analysis IDs or redundant offer to explain."
    )
    return await answer_with_review(
        executor=executor, question=question, history="", hints="Daily briefing; not a chat query.",
        answerer=answerer or GroundedAnswerAgent(user_id=principal.household_user_id),
        reviewer=reviewer or AnswerCoverageAgent(user_id=principal.household_user_id),
    )
