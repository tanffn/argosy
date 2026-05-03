"""DomainRefreshAgent tests."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.domain_refresh import (
    CitedSource,
    DomainRefreshAgent,
    DomainRefreshReport,
    FileRefreshResult,
)


class _MockDomainRefreshAgent(DomainRefreshAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=400,
            tokens_out=600,
            model=self.model,
        )


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
