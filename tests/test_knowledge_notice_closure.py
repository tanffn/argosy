import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from argosy.services import knowledge_notice_closure as closure
from argosy.state import db as db_mod
from argosy.state.models import ActionProposal, AuditLog, User


async def seed():
    async with db_mod.get_session() as session:
        session.add(User(id='ariel'))
        await session.flush()
        row = ActionProposal(user_id='ariel', summary='old notice', rationale_md='old rationale',
            suggested_payload=json.dumps({'discrepancies': [{'path': 'test.md', 'diff': 'old'}]}),
            severity='warning', expires_at=datetime.now(UTC) + timedelta(days=1),
            status='open', kind='note_only', execution_state='proposed',
            dedup_key='domain_refresh_discrepancies:ariel')
        session.add(row)
        await session.commit()
        return row.id


def setup_packet(monkeypatch, *, matching=True):
    from argosy.orchestrator.loops import annual
    monkeypatch.setattr(annual, '_default_files_provider', lambda: [{'path': 'test.md', 'body': 'fixed'}])
    async def status(*args, **kwargs):
        return {'documents': [{'path': 'test.md', 'input_matches': matching, 'state': 'partial'}]}
    async def persist(report):
        return 987
    monkeypatch.setattr(closure, 'collect_knowledge_status', status)
    monkeypatch.setattr(annual, '_persist_refresh_report', persist)


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition', ['superseded', 'still_open'])
async def test_fleet_disposition_preserves_original_and_is_idempotent(engine, monkeypatch, disposition):
    row_id = await seed()
    setup_packet(monkeypatch)
    calls = []
    class Reviewer:
        async def run(self, *, packet):
            calls.append(packet)
            return SimpleNamespace(output=closure.NoticeVerdict(proposal_id=row_id,
                disposition=disposition, covered_paths=['test.md'], rationale='Evidence explained'))
    result = await closure.reconcile_knowledge_notices(user_id='ariel', reviewer_factory=Reviewer)
    assert result[0]['applied'] == (disposition == 'superseded')
    assert await closure.reconcile_knowledge_notices(user_id='ariel', reviewer_factory=Reviewer) == []
    assert len(calls) == 1
    async with db_mod.get_session() as session:
        row = await session.get(ActionProposal, row_id)
        assert row.summary == 'old notice' and row.rationale_md == 'old rationale'
        assert json.loads(row.suggested_payload)['discrepancies'][0]['diff'] == 'old'
        assert row.status == ('superseded' if disposition == 'superseded' else 'open')
        assert row.execution_state == ('dismissed' if disposition == 'superseded' else 'proposed')
        assert len((await session.execute(select(AuditLog))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['accepted', 'evidence', 'coverage'])
async def test_rejects_stale_or_wrong_judgment(engine, monkeypatch, mutation):
    row_id = await seed()
    setup_packet(monkeypatch)
    class Reviewer:
        async def run(self, *, packet):
            if mutation == 'accepted':
                async with db_mod.get_session() as session:
                    row = await session.get(ActionProposal, row_id)
                    row.status = 'accepted'
                    row.execution_state = 'accepted_pending_user_action'
                    await session.commit()
            elif mutation == 'evidence':
                setup_packet(monkeypatch, matching=False)
            return SimpleNamespace(output=closure.NoticeVerdict(proposal_id=row_id,
                disposition='superseded', covered_paths=['wrong.md' if mutation == 'coverage' else 'test.md'],
                rationale='Evidence explained'))
    if mutation == 'coverage':
        with pytest.raises(ValueError, match='coverage'):
            await closure.reconcile_knowledge_notices(user_id='ariel', reviewer_factory=Reviewer)
    else:
        result = await closure.reconcile_knowledge_notices(user_id='ariel', reviewer_factory=Reviewer)
        assert not result[0]['applied'] and not result[0]['inputs_unchanged']
    async with db_mod.get_session() as session:
        row = await session.get(ActionProposal, row_id)
        assert row.status == ('accepted' if mutation == 'accepted' else 'open')


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('kind', 'rebalance'), ('user_id', 'someone'),
    ('dedup_key', 'domain_refresh_discrepancies:ariel_extra'), ('status', 'accepted'),
    ('execution_state', 'accepted_pending_user_action'), ('suggested_payload', '{}')])
async def test_scope_refuses_non_maintenance_actions(engine, monkeypatch, field, value):
    row_id = await seed()
    setup_packet(monkeypatch)
    async with db_mod.get_session() as session:
        row = await session.get(ActionProposal, row_id)
        setattr(row, field, value)
        with session.no_autoflush:
            assert await closure._packet(session, row, 'ariel') is None


@pytest.mark.asyncio
async def test_unreviewed_input_never_sent_to_closure_judge(engine, monkeypatch):
    await seed()
    setup_packet(monkeypatch, matching=False)
    def never():
        raise AssertionError('Must not run judge with stale review')
    assert await closure.reconcile_knowledge_notices(user_id='ariel', reviewer_factory=never) == []
