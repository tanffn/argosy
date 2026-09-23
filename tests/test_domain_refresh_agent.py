"""DomainRefreshAgent tests."""

from __future__ import annotations


import json
from typing import Any

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.domain_refresh import (
    CitedSource,
    DomainRefreshAgent,
    DomainRefreshReport,
    FileRefreshResult,
)


def test_finding_urgency_is_explicit_and_backwards_compatible():
    from argosy.agents.domain_refresh import KnowledgeFinding
    finding = dict(scope="household_fact", claim="Dated balance", missing_evidence="Statement",
                   owner="user", next_action="Upload at year end", affected_advice="Exact reconciliation")
    assert KnowledgeFinding(**finding).urgency == "routine"
    assert KnowledgeFinding(**finding, urgency="urgent").urgency == "urgent"


class _MockDomainRefreshAgent(DomainRefreshAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=400,
            tokens_out=600,
            model=self.model,
        )


def test_model_report_cannot_recover_unrelated_objects_as_empty_report():
    agent = DomainRefreshAgent(user_id='ariel')
    assert agent.use_structured_output is True
    assert 'per_file' in agent.output_model.model_json_schema()['required']
    assert '"format"' not in json.dumps(agent.output_model.model_json_schema())
    # Empty local combination and explicit no-work result are legitimate;
    # annual separately checks requested-file coverage.
    assert DomainRefreshReport().per_file == []
    assert agent._parse_output('{"per_file": []}').per_file == []
    for text in ('{}', 'broken {"url":"https://example.org"}'):
        with pytest.raises(ValueError):
            agent._parse_output(text)


def test_wire_annotation_compatibility_does_not_relax_local_date_validation():
    agent = DomainRefreshAgent(user_id='ariel')
    item = {'path': 'domain_knowledge/test.md', 'status': 'no_change',
            'next_refresh_due': '2026-02-30'}
    with pytest.raises(ValueError):
        agent.output_model.model_validate({'per_file': [item]})
    item['next_refresh_due'] = '2026-12-31'
    assert agent.output_model.model_validate({'per_file': [item]}).per_file[0].next_refresh_due.isoformat() == '2026-12-31'


def test_model_report_recovers_actual_report_not_preceding_evidence_object():
    agent = DomainRefreshAgent(user_id='ariel')
    payload = {'per_file': [{'path': 'domain_knowledge/test.md', 'status': 'no_change',
                            'verification': 'unavailable', 'note': 'Source unavailable'}]}
    text = 'Malformed preliminary object {"url":"https://example.org"}\n' + json.dumps(payload)
    assert agent._parse_output(text).per_file[0].path == 'domain_knowledge/test.md'


@pytest.mark.asyncio
async def test_domain_refresh_report_no_change() -> None:
    canned = {
        "per_file": [
            {
                "path": "domain_knowledge/tax/israel/capital_gains.md",
                "status": "no_change",
                "diff": None,
                "evidence": [
                    {
                        "url": "https://taxes.gov.il/...",
                        "retrieved_at": "2026-05-02",
                        "excerpt": "Capital gains rate remains 25%.",
                        "tier": 1,
                    }
                ],
                "next_refresh_due": "2026-08-01",
                "note": "Verified — no change.",
            }
        ],
        "summary": "1 file checked; no change.",
        "confidence": "HIGH",
        "cited_sources": ["https://taxes.gov.il/..."],
    }
    agent = _MockDomainRefreshAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        files_due=[
            {
                "path": "domain_knowledge/tax/israel/capital_gains.md",
                "frontmatter": "next_refresh_due: 2026-05-01",
                "content": "Israeli capital gains tax: 25%.",
            }
        ],
    )
    out = report.output
    assert isinstance(out, DomainRefreshReport)
    assert len(out.per_file) == 1
    r = out.per_file[0]
    assert isinstance(r, FileRefreshResult)
    assert r.status == "no_change"
    assert r.diff is None
    assert isinstance(r.evidence[0], CitedSource)
    assert r.evidence[0].tier == 1


@pytest.mark.asyncio
async def test_domain_refresh_report_change_proposed() -> None:
    canned = {
        "per_file": [
            {
                "path": "domain_knowledge/tax/israel/dividend_withholding.md",
                "status": "change_proposed",
                "diff": "- 25%\n+ 30% (effective 2026-06-01)",
                "evidence": [
                    {
                        "url": "https://taxes.gov.il/circular/2026-15",
                        "retrieved_at": "2026-05-02",
                        "excerpt": "Effective 1 June 2026, dividend withholding rises to 30%.",
                        "tier": 1,
                    }
                ],
                "next_refresh_due": None,
                "note": "Tier-1 source; needs human review.",
            }
        ],
        "summary": "1 file flagged for change.",
        "confidence": "HIGH",
        "cited_sources": ["https://taxes.gov.il/circular/2026-15"],
    }
    agent = _MockDomainRefreshAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        files_due=[
            {
                "path": "domain_knowledge/tax/israel/dividend_withholding.md",
                "frontmatter": "next_refresh_due: 2026-04-01",
                "content": "Dividend withholding: 25%.",
            }
        ],
    )
    out = report.output
    assert out.per_file[0].status == "change_proposed"
    assert "30%" in (out.per_file[0].diff or "")


def test_domain_refresh_has_web_tools_and_demands_citations() -> None:
    """Root-cause guard (verify-run 2026-07-08): the prompt requires live
    re-fetches, so the claude_code backend must GRANT the web tools — a
    tool-less run had the model refuse to fabricate verification, leaving
    `cited_sources` empty and failing the citation gate on every annual tick.
    The prompt must also demand a non-empty top-level `cited_sources`."""
    assert "WebSearch" in DomainRefreshAgent.claude_code_allowed_tools
    assert "WebFetch" in DomainRefreshAgent.claude_code_allowed_tools
    # Evidence eligibility lives at per-file verification/writeback now.
    assert DomainRefreshAgent.require_citations is False  # Unavailable reports persist; cannot stamp.

    agent = DomainRefreshAgent(user_id="ariel")
    system, _user = agent.build_prompt(
        files_due=[{"path": "x.md", "frontmatter": "", "content": "y"}]
    )
    assert "CITATIONS ARE MANDATORY" in system
    assert "cited_sources" in system
    assert "Copy the exact requested input path including its domain_knowledge/ prefix" in system
    assert "Selected PDF excerpts cover ONLY" in system


@pytest.mark.asyncio
async def test_domain_refresh_unavailable_sources_produce_explicit_incomplete_report() -> None:
    """Unavailable evidence is retained for the loop, never falsely verified."""

    canned = {
        "per_file": [
            {
                "path": "domain_knowledge/tax/israel/capital_gains.md",
                "status": "no_change",
                "diff": None,
                "evidence": [],
                "next_refresh_due": "2026-08-01",
                "note": "unverified",
            }
        ],
        "summary": "1 file checked; no sources fetched.",
        "confidence": "LOW",
        "cited_sources": [],
    }
    agent = _MockDomainRefreshAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
            files_due=[
                {
                    "path": "domain_knowledge/tax/israel/capital_gains.md",
                    "frontmatter": "",
                    "content": "Israeli capital gains tax: 25%.",
                }
            ],
        )
    assert report.output.per_file[0].verification == "unavailable"


@pytest.mark.asyncio
async def test_domain_refresh_no_files_returns_empty_list() -> None:
    canned = {
        "per_file": [],
        "summary": "No files due.",
        "confidence": "HIGH",
        "cited_sources": ["domain_knowledge/_meta/sources.md"],
    }
    agent = _MockDomainRefreshAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(files_due=[])
    assert report.output.per_file == []
