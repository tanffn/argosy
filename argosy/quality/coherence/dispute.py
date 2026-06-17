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


# reader finding kind -> dispute conflict_type
_KIND_TO_CONFLICT = {
    "contradiction": "value_mismatch",
    "cross_surface": "value_mismatch",
    "calc_inconsistency": "calc_inconsistency",
    "stale": "value_mismatch",
    "regression": "value_mismatch",
    "fragile_claim": "policy_tension",   # goal/framing tension
    "other": "policy_tension",
}


def cluster_findings(findings: list[dict]) -> list[Dispute]:
    """Group typed reader findings into one Dispute per (subject_type, conflict_type).
    Surface ids accumulate as evidence. A finding with no subject_type becomes an
    untyped dispute (the router will BLOCK it)."""
    grouped: dict[tuple[str, str], dict] = {}
    for f in findings:
        subject = (f.get("subject_type") or "").strip()
        conflict = _KIND_TO_CONFLICT.get(f.get("kind", "other"), "policy_tension")
        gk = (subject, conflict)
        g = grouped.setdefault(gk, {"surfaces": set(), "fields": set(), "options": set(),
                                    "questions": []})
        g["surfaces"].update(f.get("surfaces_cited") or [])
        if f.get("field_path"):
            g["fields"].add(f["field_path"])
        if f.get("normalized_claim"):
            g["options"].add(f["normalized_claim"])
        g["questions"].append(f.get("detail") or "")
    out: list[Dispute] = []
    for (subject, conflict), g in grouped.items():
        out.append(Dispute(
            subject_type=subject,
            subject_field_path=sorted(g["fields"])[0] if g["fields"] else "",
            scope="person", conflict_type=conflict,
            normalized_options=tuple(sorted(g["options"])),
            question=g["questions"][0] if g["questions"] else "",
            surfaces_cited=tuple(sorted(g["surfaces"])),
        ))
    return out
