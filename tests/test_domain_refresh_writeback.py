"""Domain-refresh write-back tests (2026-07-08 systemic-gap fix).

Covers:
  - `apply_refresh_to_frontmatter` — pure frontmatter stamping: updates
    `last_verified` + matched sources' `retrieved`, byte-identical body,
    idempotent, unmatched sources untouched, CRLF preserved.
  - `write_back_refresh_results` — file resolution + write, missing paths
    recorded, change_proposed content never rewritten.
  - AnnualLoop wiring — stubbed agent: frontmatter stamped, agent_reports
    row + output blob persisted, ONE aggregated discrepancy proposal
    (idempotent per dedup_key).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.domain_refresh import (
    CitedSource,
    DomainRefreshAgent,
    DomainRefreshReport,
    FileRefreshResult,
    apply_refresh_to_frontmatter,
    write_back_refresh_results,
)
from argosy.api import events
from argosy.orchestrator.cost_guard import reset_cost_guard
from argosy.orchestrator.loops.annual import AnnualLoop
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import ActionProposal, AgentReport, AgentReportBlob, User

SAMPLE = (
    "---\n"
    "title: Test memo\n"
    "last_verified: 1900-01-01\n"
    "next_refresh_due: 2026-01-01\n"
    "source_urls:\n"
    "  - https://example.com/a\n"
    "sources:\n"
    "  - url: https://example.com/a\n"
    "    retrieved: 1900-01-01\n"
    "    tier: 1\n"
    "  - url: https://example.com/b\n"
    "    retrieved: 1900-01-01\n"
    "    tier: 2\n"
    "---\n"
    "\n"
    "# Body\n"
    "\n"
    "> last_verified: 1900-01-01 — body mentions must NOT be rewritten.\n"
    "retrieved: 1900-01-01 in the body stays too.\n"
)

TODAY = date(2026, 7, 8)


def test_apply_updates_last_verified_and_matched_source_only() -> None:
    out = apply_refresh_to_frontmatter(
        SAMPLE, verified_on=TODAY, consulted_urls=["https://example.com/a"]
    )
    fm, body = out.split("\n---\n", 1)
    assert "last_verified: 2026-07-08" in fm
    assert "last_verified: 1900-01-01" not in fm
    # Matched source stamped; unmatched keeps its sentinel.
    assert (
        "  - url: https://example.com/a\n    retrieved: 2026-07-08\n" in out
    )
    assert (
        "  - url: https://example.com/b\n    retrieved: 1900-01-01\n" in out
    )
    # Body byte-identical (incl. the decoy mentions).
    _, orig_body = SAMPLE.split("\n---\n", 1)
    assert body == orig_body
    # Untouched frontmatter keys preserved.
    assert "next_refresh_due: 2026-01-01" in fm
    assert "source_urls:\n  - https://example.com/a" in fm


def test_apply_is_idempotent() -> None:
    once = apply_refresh_to_frontmatter(
        SAMPLE, verified_on=TODAY, consulted_urls=["https://example.com/a"]
    )
    twice = apply_refresh_to_frontmatter(
        once, verified_on=TODAY, consulted_urls=["https://example.com/a"]
    )
    assert once == twice


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_source_without_url_does_not_inherit_previous_url_date(newline) -> None:
    from argosy.services.knowledge_status import input_fingerprint
    original = ('---\nlast_verified: 2026-09-08\nsources:\n'
        '  - url: https://example.com/rule\n    retrieved: 2026-09-12\n'
        '  - source: User declaration\n    retrieved: 2026-08-23\n'
        '    tier: 1\n---\n\nHistorical declaration stays historical.\n')
    rendered = apply_refresh_to_frontmatter(original.replace('\n', newline),
        verified_on=date(2026, 9, 13), consulted_urls=['https://example.com/rule'])
    normalized = rendered.replace('\r\n', '\n')
    assert 'url: https://example.com/rule\n    retrieved: 2026-09-13' in normalized
    assert 'source: User declaration\n    retrieved: 2026-08-23' in normalized
    def fingerprint(raw):
        end = raw.find('\n---\n', 4)
        return input_fingerprint({'frontmatter': raw[4:end], 'content': raw[end+5:]})
    assert fingerprint(original) == fingerprint(normalized)
    assert apply_refresh_to_frontmatter(rendered, verified_on=date(2026, 9, 13),
        consulted_urls=['https://example.com/rule']) == rendered


def test_apply_url_match_is_slash_and_case_insensitive() -> None:
    out = apply_refresh_to_frontmatter(
        SAMPLE,
        verified_on=TODAY,
        consulted_urls=["HTTPS://EXAMPLE.COM/A/"],
    )
    assert (
        "  - url: https://example.com/a\n    retrieved: 2026-07-08\n" in out
    )


def test_apply_no_frontmatter_returns_unchanged() -> None:
    plain = "# Just a doc\nlast_verified: 1900-01-01\n"
    assert (
        apply_refresh_to_frontmatter(plain, verified_on=TODAY, consulted_urls=[])
        == plain
    )
    unclosed = "---\nlast_verified: 1900-01-01\nno closing delimiter\n"
    assert (
        apply_refresh_to_frontmatter(
            unclosed, verified_on=TODAY, consulted_urls=[]
        )
        == unclosed
    )


def test_apply_preserves_crlf_line_endings() -> None:
    crlf = SAMPLE.replace("\n", "\r\n")
    out = apply_refresh_to_frontmatter(
        crlf, verified_on=TODAY, consulted_urls=["https://example.com/a"]
    )
    assert "last_verified: 2026-07-08\r\n" in out
    assert "\n" not in out.replace("\r\n", "")  # every LF is part of a CRLF


def _make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "domain_knowledge"
    (root / "tax").mkdir(parents=True)
    (root / "tax" / "memo.md").write_text(SAMPLE, encoding="utf-8")
    (root / "tax" / "other.md").write_text(SAMPLE, encoding="utf-8")
    return root


def _report(per_file: list[FileRefreshResult]) -> DomainRefreshReport:
    return DomainRefreshReport(
        per_file=per_file,
        summary="test",
        cited_sources=["https://example.com/a"],
    )


def test_write_back_updates_files_and_records_missing(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    report = _report(
        [
            FileRefreshResult(
                path="domain_knowledge/tax/memo.md",
                status="no_change",
                verification="verified",
                evidence=[
                    CitedSource(url="https://example.com/a", retrieved_at="2026-07-08")
                ],
            ),
            FileRefreshResult(
                path="domain_knowledge/tax/gone.md",
                status="no_change",
            ),
        ]
    )
    out = write_back_refresh_results(report, root=root, verified_on=TODAY)
    assert out["updated"] == ["domain_knowledge/tax/memo.md"]
    assert out["missing"] == ["domain_knowledge/tax/gone.md"]
    assert out["changes_proposed"] == []

    updated = (root / "tax" / "memo.md").read_text(encoding="utf-8")
    assert "last_verified: 2026-07-08" in updated
    # The file NOT in the report is untouched.
    assert (root / "tax" / "other.md").read_text(encoding="utf-8") == SAMPLE


def test_write_back_change_proposed_keeps_old_claims_unverified(
    tmp_path: Path,
) -> None:
    root = _make_tree(tmp_path)
    report = _report(
        [
            FileRefreshResult(
                path="domain_knowledge/tax/memo.md",
                status="change_proposed",
                diff="-old value\n+new value",
                note="ceiling changed for 2026",
                evidence=[
                    CitedSource(url="https://example.com/b", retrieved_at="2026-07-08")
                ],
            )
        ]
    )
    out = write_back_refresh_results(report, root=root, verified_on=TODAY)
    assert out["changes_proposed"] == ["domain_knowledge/tax/memo.md"]
    assert out["updated"] == []

    updated = (root / "tax" / "memo.md").read_text(encoding="utf-8")
    # Old claims are not made current merely because a correction was proposed.
    assert "last_verified: 1900-01-01" in updated
    # ... but the body/claims are byte-identical — no auto-edit.
    _, orig_body = SAMPLE.split("\n---\n", 1)
    assert updated.split("\n---\n", 1)[1] == orig_body
    assert "new value" not in updated


def test_write_back_is_idempotent(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    report = _report(
        [
            FileRefreshResult(
                path="domain_knowledge/tax/memo.md",
                status="no_change",
                verification="verified",
                evidence=[
                    CitedSource(url="https://example.com/a", retrieved_at="2026-07-08")
                ],
            )
        ]
    )
    first = write_back_refresh_results(report, root=root, verified_on=TODAY)
    assert first["updated"] == ["domain_knowledge/tax/memo.md"]
    second = write_back_refresh_results(report, root=root, verified_on=TODAY)
    assert second["updated"] == []
    assert second["unchanged"] == ["domain_knowledge/tax/memo.md"]


def test_write_back_rejects_traversal_outside_root(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    outside = tmp_path / "secret.md"
    outside.write_text(SAMPLE, encoding="utf-8")
    report = _report(
        [FileRefreshResult(path="../secret.md", status="no_change")]
    )
    out = write_back_refresh_results(report, root=root, verified_on=TODAY)
    assert out["missing"] == ["../secret.md"]
    assert outside.read_text(encoding="utf-8") == SAMPLE


@pytest.mark.parametrize("verification", ["partial", "unavailable"])
def test_incomplete_source_verification_never_stamps_dates(tmp_path, verification):
    root = _make_tree(tmp_path)
    report = _report([FileRefreshResult(path="domain_knowledge/tax/memo.md",
        status="no_change", verification=verification,
        evidence=[CitedSource(url="https://example.com/a", retrieved_at="2026-07-08")])])
    out = write_back_refresh_results(report, root=root, verified_on=TODAY)
    assert out["updated"] == []
    assert out["unverified"] == ["domain_knowledge/tax/memo.md"]
    assert (root / "tax/memo.md").read_text(encoding="utf-8") == SAMPLE


def test_legacy_report_and_verified_without_evidence_cannot_stamp(tmp_path):
    root = _make_tree(tmp_path)
    for entry in (FileRefreshResult(path="tax/memo.md", status="no_change"),
                  FileRefreshResult(path="tax/memo.md", status="no_change", verification="verified")):
        assert not write_back_refresh_results(_report([entry]), root=root)["updated"]


@pytest.mark.parametrize("url,retrieved", [(" ", "2026-07-08"), ("https://example.com", ""),
                                          ("https://example.com", "2026-02-31")])
def test_invalid_source_provenance_is_rejected(url, retrieved):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CitedSource(url=url, retrieved_at=retrieved)


def test_old_evidence_or_edited_document_never_gets_fresh_stamp(tmp_path):
    root = _make_tree(tmp_path)
    entry = FileRefreshResult(path="domain_knowledge/tax/memo.md", status="no_change", verification="verified",
                              evidence=[CitedSource(url="https://example.com/a", retrieved_at="2026-07-08")])
    assert not write_back_refresh_results(_report([entry]), root=root, verified_on=date(2026, 7, 9))["updated"]
    out = write_back_refresh_results(_report([entry]), root=root, verified_on=TODAY,
                                     source_hashes={entry.path: "old hash"})
    assert out["changed_since_review"] == [entry.path]
    assert (root / "tax/memo.md").read_text(encoding="utf-8") == SAMPLE


# ---------------------------------------------------------------------------
# AnnualLoop wiring (stubbed agent)
# ---------------------------------------------------------------------------

_CANNED = {
    "per_file": [
        {
            "path": "domain_knowledge/tax/memo.md",
            "status": "no_change",
            "verification": "verified",
            "diff": None,
            "evidence": [
                {
                    "url": "https://example.com/a",
                    "retrieved_at": date.today().isoformat(),
                    "excerpt": "still 25%",
                    "tier": 1,
                }
            ],
            "next_refresh_due": "2027-01-31",
            "note": "verified",
        },
        {
            "path": "domain_knowledge/tax/other.md",
            "status": "change_proposed",
            "verification": "verified",
            "diff": "-ceiling 20000\n+ceiling 21000",
            "evidence": [
                {
                    "url": "https://example.com/b",
                    "retrieved_at": date.today().isoformat(),
                    "excerpt": "ceiling raised",
                    "tier": 1,
                }
            ],
            "next_refresh_due": "2027-01-31",
            "note": "2026 ceiling changed",
        },
    ],
    "summary": "2 files checked; 1 discrepancy.",
    "confidence": "HIGH",
    "cited_sources": ["https://example.com/a", "https://example.com/b"],
}


def _stub_refresh_factory():
    class _M(DomainRefreshAgent):
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
            selected = [row for row in _CANNED["per_file"] if row["path"] in user]
            return ModelCall(
                text=json.dumps({**_CANNED, "per_file": selected}),
                tokens_in=100,
                tokens_out=200,
                model=self.model,
            )

    return _M(user_id="ariel")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["runtime", "partial"])
async def test_annual_preserves_other_documents_and_reports_incomplete(engine, tmp_path, failure):
    from copy import deepcopy
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()
    reset_cost_guard()
    root = _make_tree(tmp_path)
    seen = []
    class Agent(DomainRefreshAgent):
        async def _call_model(self, *, system, user, **kwargs):
            row = deepcopy(next(r for r in _CANNED["per_file"] if r["path"] in user))
            seen.append(row["path"])
            if "memo.md" in row["path"]:
                if failure == "runtime":
                    raise RuntimeError("deliberate unavailable source")
                row["verification"] = "partial"
                row["note"] = "Source document could not be fetched"
            else:
                row.update(status="no_change", diff=None)
            return ModelCall(text=json.dumps({**_CANNED, "per_file": [row]}),
                             tokens_in=100, tokens_out=100, model=self.model)
    loop = AnnualLoop(schedule=LoopSchedule(cron="0 8 2 1 *"),
        domain_refresh_factory=lambda: Agent(user_id="ariel"),
        domain_files_provider=lambda: [{"path": r["path"], "frontmatter": "", "content": "test"}
                                      for r in _CANNED["per_file"]], domain_knowledge_root=root)
    with pytest.raises(RuntimeError, match="verification_incomplete"):
        await loop.tick()
    assert len(seen) == 2
    assert len(loop.last_output_summary["domain_refresh_files"]) == 2
    assert loop.last_output_summary["domain_refresh_writeback"]["updated"] == ["domain_knowledge/tax/other.md"]
    assert (root / "tax/memo.md").read_text(encoding="utf-8") == SAMPLE


@pytest.mark.asyncio
async def test_wrong_coverage_keeps_persisted_report_link(engine, tmp_path):
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()
    reset_cost_guard()
    root = _make_tree(tmp_path)
    class Wrong(DomainRefreshAgent):
        async def _call_model(self, **kwargs):
            return ModelCall(text=json.dumps(_CANNED), tokens_in=1, tokens_out=1, model=self.model)
    loop = AnnualLoop(schedule=LoopSchedule(cron="0 8 2 1 *"),
        domain_refresh_factory=lambda: Wrong(user_id="ariel"),
        domain_files_provider=lambda: [{"path": "domain_knowledge/tax/memo.md", "frontmatter": "", "content": "test"}],
        domain_knowledge_root=root)
    with pytest.raises(RuntimeError, match="exactly the requested"):
        await loop.tick()
    row = loop.last_output_summary["domain_refresh_files"][0]
    assert row["report_id"] in loop.last_output_summary["domain_refresh_report_ids"]
    assert row["verification"] == "unavailable"


@pytest.mark.asyncio
async def test_unverified_change_is_not_published_as_a_verified_discrepancy(engine):
    from argosy.orchestrator.loops.annual import _surface_refresh_discrepancies
    from datetime import datetime, timezone
    report = _report([FileRefreshResult(path="tax/memo.md", status="change_proposed",
        verification="partial", note="Search lead only", diff="Unverified candidate")])
    assert await _surface_refresh_discrepancies(user_id="ariel", output=report,
        now=datetime.now(timezone.utc)) == 0


@pytest.mark.asyncio
async def test_annual_loop_writes_back_persists_and_surfaces(
    engine: None, tmp_path: Path
) -> None:
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    root = _make_tree(tmp_path)
    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_stub_refresh_factory,
        domain_files_provider=lambda: [
            {"path": "domain_knowledge/tax/memo.md", "frontmatter": "", "content": "x"},
            {"path": "domain_knowledge/tax/other.md", "frontmatter": "", "content": "y"},
        ],
        domain_knowledge_root=root,
    )
    summary = await loop.tick()
    assert summary is not None
    assert summary["steps"]["domain_refresh"] == "ok"

    # 1. Only verified unchanged claims receive a current verification stamp.
    today = date.today().isoformat()
    memo = (root / "tax" / "memo.md").read_text(encoding="utf-8")
    other = (root / "tax" / "other.md").read_text(encoding="utf-8")
    assert f"last_verified: {today}" in memo
    assert "last_verified: 1900-01-01" in other
    # Consulted source stamped, the other source keeps its sentinel.
    assert f"  - url: https://example.com/a\n    retrieved: {today}\n" in memo
    assert "  - url: https://example.com/b\n    retrieved: 1900-01-01\n" in memo
    # change_proposed content NOT rewritten.
    assert "ceiling 21000" not in other
    assert summary["domain_refresh_writeback"]["updated"] == [
        "domain_knowledge/tax/memo.md",
    ]

    # 2. agent_reports row + output blob persisted.
    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AgentReport).where(AgentReport.agent_role == "domain_refresh")
            )
        ).scalars().all()
        assert len(rows) == 2
        assert summary["domain_refresh_report_ids"] == [r.id for r in rows]
        blob = (
            await session.execute(
                select(AgentReportBlob).where(
                    AgentReportBlob.report_id == rows[0].id,
                    AgentReportBlob.key == "output_json",
                )
            )
        ).scalars().one()
        assert "2 files checked" in blob.value

    # 3. ONE aggregated discrepancy proposal, idempotent per dedup_key.
    async with db_mod.get_session() as session:
        props = (
            await session.execute(
                select(ActionProposal).where(
                    ActionProposal.dedup_key
                    == "domain_refresh_discrepancies:ariel"
                )
            )
        ).scalars().all()
    assert len(props) == 1
    assert props[0].kind == "note_only"
    assert props[0].status == "open"
    assert "other.md" in props[0].rationale_md
    assert "2026 ceiling changed" in props[0].rationale_md
    assert summary["domain_refresh_discrepancies"] == 1

    # Second tick: proposal refreshed in place — still exactly one open row.
    await loop.tick()
    async with db_mod.get_session() as session:
        props = (
            await session.execute(
                select(ActionProposal).where(
                    ActionProposal.dedup_key
                    == "domain_refresh_discrepancies:ariel"
                )
            )
        ).scalars().all()
    assert len(props) == 1


@pytest.mark.asyncio
async def test_annual_loop_no_discrepancies_creates_no_proposal(
    engine: None, tmp_path: Path
) -> None:
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    canned = {
        **_CANNED,
        "per_file": [_CANNED["per_file"][0]],
        "summary": "1 file checked.",
    }

    def _factory():
        class _M(DomainRefreshAgent):
            async def _call_model(
                self, *, system: str, user: str, **_extra: Any
            ) -> ModelCall:
                return ModelCall(
                    text=json.dumps(canned),
                    tokens_in=100,
                    tokens_out=200,
                    model=self.model,
                )

        return _M(user_id="ariel")

    root = _make_tree(tmp_path)
    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_factory,
        domain_files_provider=lambda: [
            {"path": "domain_knowledge/tax/memo.md", "frontmatter": "", "content": "x"}
        ],
        domain_knowledge_root=root,
    )
    summary = await loop.tick()
    assert summary is not None
    assert summary["domain_refresh_discrepancies"] == 0

    async with db_mod.get_session() as session:
        props = (
            await session.execute(select(ActionProposal))
        ).scalars().all()
    assert props == []
