from types import SimpleNamespace

import pytest

from argosy.agents.evidence_review import Calculation, EvidenceAudit, EvidenceGrounding
from argosy.services.evidence_review import EvidenceReviewFailed, reviewed_authoring


def calculation(claimed="100"):
    return Calculation(description="Upside", operation="percent_change", operands=["30", "60"],
                       claimed_result=claimed, source_ids=["raw"], scope_and_units="percent")


def test_arithmetic_rederived_not_model_agreement():
    assert calculation().check()["matches"]
    assert not calculation("10").check()["matches"]
    assert calculation("10").check()["computed_result"] == "100"


@pytest.mark.parametrize("operation,operands,claim", [
    ("ratio", ["54", "100"], "54.0%"), ("percent_of", ["54", "100"], "54.0"),
    ("sum", ["20", "30", "50"], "100%"),
    ("percent_change", ["100", "120"], "20%")])
def test_explicit_percentage_units_are_checked_not_confused_with_fractions(operation, operands, claim):
    assert calculation().model_copy(update={"operation": operation, "operands": operands,
                                            "claimed_result": claim}).check()["matches"]


@pytest.mark.parametrize("values", [["0", "1"], ["NaN", "2"], ["1e999", "2"]])
def test_bad_arithmetic_is_explicit(values):
    calc = calculation().model_copy(update={"operands": values})
    assert not calc.check()["matches"]


class Stub:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output=next(self.outputs), blobs={})


@pytest.mark.asyncio
async def test_no_action_is_reviewed_and_reauthored_after_wrong_math():
    grounder = Stub([EvidenceGrounding(facts=["entry30, target60"], cited_sources=["raw"])])
    reviewer = Stub([EvidenceAudit(approved=True, calculations=[calculation("10")], arithmetic_coverage="complete", arithmetic_coverage_reason="all"),
                     EvidenceAudit(approved=True, calculations=[calculation()], arithmetic_coverage="complete", arithmetic_coverage_reason="all")])
    drafts, saved = [], []
    async def author(feedback):
        drafts.append(feedback)
        return SimpleNamespace(output=EvidenceGrounding(facts=["HOLD"]),
                               sources_json='[{"source_id":"reader:claims","content":"secondary claims"}]',
                               user_prompt="author input")
    async def persist(report):
        saved.append(report)
    final, proof = await reviewed_authoring(user_id="test", sources=[("raw", "entry30 target60")],
        context="Unowned candidate", author=author, persist=persist,
        grounder=grounder, reviewer=reviewer)
    assert "draft" not in grounder.calls[0]  # genuinely blind
    assert grounder.calls[0]["sources"] == [("raw", "entry30 target60")]
    assert ("reader:claims", "secondary claims") in reviewer.calls[0]["sources"]
    assert ("review:author_input_packet", "author input") in reviewer.calls[0]["sources"]
    assert "deterministic_arithmetic" in saved[-1].blobs
    assert len(drafts) == 2 and "100" in drafts[-1]
    assert len(saved) == 5 and proof["status"] == "approved"


@pytest.mark.asyncio
async def test_unresolved_review_never_publishes_a_final_verdict():
    async def author(feedback):
        return SimpleNamespace(output=EvidenceGrounding(facts=["PASS"]))
    async def persist(report):
        pass
    with pytest.raises(EvidenceReviewFailed):
        await reviewed_authoring(user_id="test", sources=[("raw", "evidence")], context="",
            author=author, persist=persist, max_revisions=1,
            grounder=Stub([EvidenceGrounding()]),
            reviewer=Stub([EvidenceAudit(approved=False, findings=["Unsupported dismissal"], arithmetic_coverage="none_applicable", arithmetic_coverage_reason="qualitative")] * 2))


@pytest.mark.asyncio
async def test_approval_with_missing_arithmetic_coverage_is_not_released():
    async def author(feedback):
        return SimpleNamespace(output=EvidenceGrounding(facts=["A material derived return"]), blobs={})
    async def persist(report):
        pass
    with pytest.raises(EvidenceReviewFailed):
        await reviewed_authoring(user_id="test", sources=[("raw", "evidence")], context="",
            author=author, persist=persist, max_revisions=0,
            grounder=Stub([EvidenceGrounding()]),
            reviewer=Stub([EvidenceAudit(approved=True, arithmetic_coverage="complete",
                                         arithmetic_coverage_reason="all", calculations=[])]))
