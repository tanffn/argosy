# argosy/quality/coherence/dispute.py
"""Structured dispute identity. The dispute_key is a hash over STRUCTURED fields
(never the natural-language question), computed AFTER normalization so phrasing
drift cannot mint a new key. Surface IDs are evidence, not identity."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

ConflictType = Literal[
    "value_mismatch", "policy_tension", "calc_inconsistency", "representation_mismatch"
]


@dataclass(frozen=True)
class Dispute:
    subject_type: str
    subject_field_path: str
    scope: str
    conflict_type: ConflictType
    normalized_options: tuple[str, ...] = ()
    implicated_canonical_fact_ids: tuple[str, ...] = ()
    implicated_user_directive_ids: tuple[str, ...] = ()
    question: str = ""  # human-readable; NOT part of identity
    surfaces_cited: tuple[str, ...] = ()  # evidence; NOT part of identity


def dispute_key(d: Dispute) -> str:
    """Stable identity hash over normalized structured fields only."""
    parts = [
        d.subject_type.strip().lower(),
        d.subject_field_path.strip().lower(),
        d.scope.strip().lower(),
        d.conflict_type,
        "|".join(sorted(o.strip().lower() for o in d.normalized_options)),
        "|".join(sorted(f.strip().lower() for f in d.implicated_canonical_fact_ids)),
        "|".join(sorted(x.strip().lower() for x in d.implicated_user_directive_ids)),
    ]
    raw = "\x1f".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
