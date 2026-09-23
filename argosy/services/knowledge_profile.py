"""Explicit, bounded profile evidence; stored declarations are not legal certification."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import yaml
from sqlalchemy import select

from argosy.state.models import UserContext


async def profile_evidence(session, *, user_id, item):
    metadata = yaml.safe_load(item.get('frontmatter', '')) or {}
    fields = metadata.get('profile_fields', []) if isinstance(metadata, dict) else []
    if not fields:
        return None
    if not isinstance(fields, list) or len(fields) > 20 or any(
            not isinstance(f, str) or not f or '.' in f for f in fields):
        raise ValueError('profile_fields requires at most 20 explicit top-level identity keys')
    row = (await session.execute(select(UserContext).where(UserContext.user_id == user_id))).scalars().first()
    identity = yaml.safe_load(row.identity_yaml) if row else {}
    if not isinstance(identity, dict):
        identity = {}
    values = {}
    missing = []
    for field in fields:
        value = identity.get(field)
        if value is None:
            missing.append(field)
        elif isinstance(value, (dict, list)):
            raise ValueError('Profile evidence permits scalar fields only')
        else:
            values[field] = value
    content = json.dumps({'stored_planning_facts': values, 'missing_fields': missing},
                         sort_keys=True, ensure_ascii=False, default=str)
    if len(content) > 20_000:
        raise ValueError('Profile evidence exceeds bounded content limit')
    return {'url': 'file://Profile/identity', 'content': content,
            'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
            'status': 'captured' if row else 'unavailable', 'truncated': False,
            'accessed_at': datetime.now(UTC).isoformat(),
            'record_updated_at': row.updated_at.isoformat() if row and row.updated_at else None,
            'source_as_of': None,
            'provenance_note': 'Existing user-scoped planning profile. Record update time is NOT '
                'the original declaration date. These stored facts are not independent certificates '
                'or proof of domicile history. Reuse for planning with that attribution; do not '
                'ask the user to repeat a stored answer merely because it was omitted before.'}
