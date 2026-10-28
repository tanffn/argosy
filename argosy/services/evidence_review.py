"""Bounded author/reviewer reconciliation; unresolved drafts are never published."""
from __future__ import annotations

import json

from argosy.agents.evidence_review import EvidenceGrounder, EvidenceReviewer


class EvidenceReviewFailed(RuntimeError):
    pass


async def reviewed_authoring(*, user_id, sources, context, author, persist,
                             image_attachments=None, grounder=None, reviewer=None,
                             max_revisions=2, decision_id=None, draft_payload=None):
    """author(feedback) returns AgentReport; persist records every attempted report.

    The grounder never receives the draft or inherited analyst conclusions.
    Injected agents provide test seams; production never skips this review.
    """
    grounder = grounder or EvidenceGrounder(user_id=user_id)
    reviewer = reviewer or EvidenceReviewer(user_id=user_id)
    baseline = await grounder.run(sources=sources, context=context,
                                 image_attachments=image_attachments or [], decision_id=decision_id)
    await persist(baseline)
    baseline_json = baseline.output.model_dump_json()
    feedback = "Independent original-evidence baseline:\n" + baseline_json
    audits = []
    for attempt in range(max_revisions + 1):
        draft = await author(feedback)
        await persist(draft)
        current_draft = json.dumps(draft_payload(draft)) if draft_payload else draft.output.model_dump_json()
        # Review the author's actual packet too, but never contaminate the blind
        # grounder's original-evidence pass with inherited model conclusions.
        author_sources = json.loads(getattr(draft, "sources_json", None) or "[]")
        supplemental = [(s["source_id"], s["content"]) for s in author_sources
                        if s.get("source_id") and isinstance(s.get("content"), str)
                        and s["source_id"] not in {key for key, _ in sources}]
        if getattr(draft, "user_prompt", None):
            supplemental.append(("review:author_input_packet", draft.user_prompt))
        supplemental.append(("review:corrections", feedback))
        audit = await reviewer.run(sources=[*sources, *supplemental], baseline=baseline_json,
                                   draft=current_draft, context=context,
                                   image_attachments=image_attachments or [], decision_id=decision_id)
        checks = [c.check() for c in audit.output.calculations]
        audit.blobs["deterministic_arithmetic"] = json.dumps(checks)
        await persist(audit)
        audit_payload = {**audit.output.model_dump(mode="json"), "arithmetic": checks,
                         "attempt": attempt}
        audits.append(audit_payload)
        coverage_ok = (audit.output.arithmetic_coverage == "complete" and bool(checks)) or (
            audit.output.arithmetic_coverage == "none_applicable" and not checks
            and bool(audit.output.arithmetic_coverage_reason.strip()))
        if audit.output.approved and coverage_ok and all(c["matches"] for c in checks):
            return draft, {"status": "approved", "baseline": baseline.output.model_dump(mode="json"),
                           "audits": audits}
        feedback = ("Revise the CURRENT DRAFT below against ORIGINAL evidence, not to defend its verdict. "
                    "Preserve already-corrected facts and fields. Do not regenerate from stale reader outputs "
                    "and thereby reintroduce resolved errors. All review findings are retained below. "
                    "Resolve these findings or explicitly report insufficient data; do not manufacture "
                    "agreement.\n" + baseline_json + "\nREVIEWS:\n" + json.dumps(audits)
                    + "\nCURRENT DRAFT TO AMEND:\n" + current_draft)
    raise EvidenceReviewFailed("Independent evidence review remains unresolved: " +
                               json.dumps(audits[-1], ensure_ascii=False)[:6000])


def original_sources(reports):
    """Use saved input evidence, not model summaries pretending to be raw facts."""
    sources = {}
    for report in reports:
        for source in json.loads(report.sources_json or "[]"):
            key, content = source.get("source_id"), source.get("content")
            if key and isinstance(content, str):
                if key in sources and sources[key] != content:
                    key = f"{key}:as_supplied_to:{report.agent_role}"
                sources[key] = content
    return list(sources.items())
