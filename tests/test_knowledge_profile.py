import json

import pytest

from argosy.services.knowledge_profile import profile_evidence
from argosy.services.knowledge_status import dependency_versions
from argosy.state import db as db_mod
from argosy.state.models import User, UserContext


@pytest.mark.asyncio
async def test_profile_is_explicit_user_scoped_and_dependency_versioned(engine):
    item = {'frontmatter': 'profile_fields: [tax_residency, user_date_of_birth]'}
    async with db_mod.get_session() as session:
        session.add_all([User(id='ariel'), User(id='other')])
        row = UserContext(user_id='ariel', identity_yaml='tax_residency: Israel\nprivate_unused: secret')
        session.add_all([row, UserContext(user_id='other', identity_yaml='tax_residency: elsewhere')])
        await session.commit()
        result = await profile_evidence(session, user_id='ariel', item=item)
        assert 'secret' not in result['content'] and 'elsewhere' not in result['content']
        assert json.loads(result['content'])['missing_fields'] == ['user_date_of_birth']
        assert result['source_as_of'] is None and 'NOT' in result['provenance_note']
        before = await dependency_versions(session, user_id='ariel', item=item)
        row.identity_yaml = 'tax_residency: Israel\nprivate_unused: changed'
        await session.commit()
        assert before == await dependency_versions(session, user_id='ariel', item=item)
        row.identity_yaml = 'tax_residency: changed'
        await session.commit()
        assert before != await dependency_versions(session, user_id='ariel', item=item)
        assert await profile_evidence(session, user_id='ariel', item={'frontmatter': ''}) is None
        assert (await profile_evidence(session, user_id='absent', item=item))['status'] == 'unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('fields', ['scalar', '[nested.key]', '[null]'])
async def test_profile_refuses_unbounded_or_implicit_fields(engine, fields):
    async with db_mod.get_session() as session:
        with pytest.raises(ValueError):
            await profile_evidence(session, user_id='ariel', item={'frontmatter': f'profile_fields: {fields}'})
