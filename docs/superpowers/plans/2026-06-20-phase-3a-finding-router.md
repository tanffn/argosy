# Phase 3a — Finding→Owner Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the deterministic spine of spec Phase 3 — "Compliance routes findings to owners, never rewrites": map each whole-artifact reader `CoherenceFinding` to the OWNER role accountable for fixing it (and the target figure node, when the subject is a single canonical figure), so a reader BLOCK becomes a set of owner-routed remediation items instead of a monolith veto.

**Architecture:** A new pure module `argosy/quality/finding_router.py`. Owner truth has ONE source: for a subject that IS a single canonical figure, the owner is derived from `figure_registry.owner_for(node_key)` (the Phase-1a registry); for prose/policy subjects that are not a single figure (RSU-vest policy, NVDA-tranche gate, SGLN/UCITS membership), an explicit `_SUBJECT_OWNER_FALLBACK`. A completeness invariant asserts every coherence `SUBJECT_REGISTRY` subject is routable. No LLM, no DB, no cycle mutation — this is the routing decision only; converting a routed finding to a `ChangeRequest(OBJECTION)` and dispatching the owner agent through the negotiation ladder is the Phase-3b integration follow-on.

**Tech Stack:** Python 3.12, frozen dataclasses, pytest. Reuses `figure_registry.owner_for`/`OwnerRole`, `live_surfaces.CANONICAL_SUBJECT_NODE`, `incremental_plan.SUBJECT_NODE_MAP`, `coherence.surface_registry.SUBJECT_REGISTRY`, and `change_adjudication.ChangeRequest`/`Author`/`ChangeKind`.

**Why this slice:** the pv58 live reader BLOCKers are CONTENT/coherence (age-46 vs withdrawal-through-48; NVDA tranche regression; UCITS basket) — each owned by a specific role. Routing them deterministically to that owner is the prerequisite for targeted remediation (recompute the blast radius, not regenerate the monolith — the "regeneration shuffles contradictions" fix). This slice is pure + testable now; the live owner-remediation plugs into the existing `RealLadderParticipants` seam.

---

### Task 1: subject→owner + subject→target-node maps (pure)

**Files:**
- Create: `argosy/quality/finding_router.py`
- Test: `tests/test_finding_router.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_finding_router.py
from argosy.quality.figure_registry import OwnerRole
from argosy.quality.coherence.surface_registry import SUBJECT_REGISTRY
from argosy.quality.finding_router import (
    subject_owner, subject_target_node, SUBJECT_OWNER_FALLBACK,
)


def test_figure_subjects_owner_comes_from_registry():
    # A subject that IS a single canonical figure inherits its owner from the
    # figure registry (ONE source of owner truth).
    assert subject_owner("fi_capital_sufficiency") is OwnerRole.RETIREMENT_FI
    assert subject_owner("retirement_age_headline") is OwnerRole.RETIREMENT_FI
    assert subject_owner("fi_crossing") is OwnerRole.RETIREMENT_FI
    assert subject_owner("net_worth_liquid") is OwnerRole.BALANCE_SHEET
    assert subject_owner("net_worth_total") is OwnerRole.BALANCE_SHEET
    assert subject_owner("us_situs_estate") is OwnerRole.ESTATE
    assert subject_owner("retention_at_vest") is OwnerRole.TAX
    assert subject_owner("retention_capital_track") is OwnerRole.TAX
    # target node is the RESOLVER key owner_for understands (not the net_worth.* default)
    assert subject_target_node("net_worth_liquid") == "portfolio.liquid_net_worth_nis"
    assert subject_target_node("fi_capital_sufficiency") == "retirement.fi_margin_signed_nis"


def test_prose_policy_subjects_have_explicit_owners():
    # Subjects that are NOT a single canonical figure (prose/policy) route via the
    # explicit fallback; they have no single target node.
    assert subject_owner("rsu_vest_policy") is OwnerRole.EQUITY_COMP
    assert subject_owner("tranche_execution_gate") is OwnerRole.INVESTMENT
    assert subject_owner("sgln_ucits_membership") is OwnerRole.INVESTMENT
    assert subject_target_node("rsu_vest_policy") is None


def test_every_registry_subject_is_routable():
    # Completeness: no coherence subject is un-owned (else a finding can't route).
    for subject in SUBJECT_REGISTRY:
        assert subject_owner(subject) is not None, f"unrouted subject: {subject}"


def test_unknown_subject_is_unrouted_not_crash():
    assert subject_owner("") is None
    assert subject_owner("not_a_subject") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finding_router.py -v`
Expected: FAIL — `ModuleNotFoundError: argosy.quality.finding_router`.

- [ ] **Step 3: Implement the maps**

```python
# argosy/quality/finding_router.py
"""Phase 3 spine — route a whole-artifact reader finding to its OWNER.

Spec principle 4: "Compliance is an orthogonal gate that routes, never rewrites."
A reader BLOCK is a set of findings; each finding is OWNED by exactly one role.
This module is the deterministic routing decision: subject_type -> owner (+ the
target figure node when the subject is a single canonical figure). Owner truth has
ONE source — for a figure subject it is figure_registry.owner_for(node); only
prose/policy subjects (not a single figure) carry an explicit fallback owner.

Pure: no LLM, no DB, no graph mutation. Converting a RoutedFinding to a
ChangeRequest(OBJECTION) and dispatching the owner through the negotiation ladder
is the Phase-3b integration follow-on. See
docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md (Phase 3).
"""
from __future__ import annotations

from dataclasses import dataclass

from argosy.quality.figure_registry import OwnerRole, owner_for
from argosy.quality.live_surfaces import CANONICAL_SUBJECT_NODE

# subject_type -> the RESOLVER manifest key that owner_for understands. The
# live_surfaces CANONICAL_SUBJECT_NODE defaults some subjects to a net_worth.* /
# estate.* key the OWNER_MAP doesn't enumerate; map those to the resolver key
# (the same override incremental_plan.SUBJECT_NODE_MAP uses) so owner_for resolves
# the real owner instead of the uncategorized Lead fallback.
_SUBJECT_RESOLVER_NODE: dict[str, str] = {
    "fi_capital_sufficiency": "retirement.fi_margin_signed_nis",
    "retirement_age_headline": "retirement.earliest_safe_age",
    "fi_crossing": "retirement.fi_crossing_year",
    "net_worth_liquid": "portfolio.liquid_net_worth_nis",
    "net_worth_investable": "portfolio.net_worth_nis",
    "net_worth_total": "portfolio.total_net_worth_incl_residence_nis",
    "us_situs_estate": "concentration.us_situs_estate_exposure_nis",
    "retention_at_vest": "tax.retention_at_vest_pct",
    "retention_capital_track": "tax.retention_capital_track_pct",
}

# Subjects that are NOT a single canonical figure — prose/policy contradictions
# (instrument membership, execution-gate, vest policy). They route to an owner but
# have no single target node; the owner fixes the prose/policy, not one figure.
SUBJECT_OWNER_FALLBACK: dict[str, OwnerRole] = {
    "rsu_vest_policy": OwnerRole.EQUITY_COMP,
    "tranche_execution_gate": OwnerRole.INVESTMENT,
    "sgln_ucits_membership": OwnerRole.INVESTMENT,
}


def subject_target_node(subject_type: str) -> str | None:
    """The resolver figure key a subject maps to, or None for prose/policy
    subjects (no single figure)."""
    return _SUBJECT_RESOLVER_NODE.get(subject_type)


def subject_owner(subject_type: str) -> OwnerRole | None:
    """The owner role accountable for a subject. Figure subjects inherit the owner
    from figure_registry.owner_for (one source of truth); prose/policy subjects use
    the explicit fallback. Unknown subject -> None (unrouted, never a crash)."""
    node = _SUBJECT_RESOLVER_NODE.get(subject_type)
    if node is not None:
        return owner_for(node).owner
    return SUBJECT_OWNER_FALLBACK.get(subject_type)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finding_router.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/finding_router.py tests/test_finding_router.py
git commit -m "feat(routing): subject->owner + subject->target-node maps (Phase 3a)"
```

---

### Task 2: `route_finding` / `route_verdict` + ChangeRequest conversion

**Files:**
- Modify: `argosy/quality/finding_router.py`
- Test: `tests/test_finding_router.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_finding_router.py
from argosy.quality.change_adjudication import AuthorKind, ChangeKind
from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding, WholeArtifactVerdict,
)
from argosy.quality.finding_router import (
    RoutedFinding, route_finding, route_verdict, to_change_request,
)


def _f(subject, sev="BLOCKER", detail="d"):
    return CoherenceFinding(kind="contradiction", severity=sev, detail=detail,
                            surfaces_cited=["x"], subject_type=subject)


def test_route_finding_figure_subject():
    r = route_finding(_f("retirement_age_headline"))
    assert isinstance(r, RoutedFinding)
    assert r.owner is OwnerRole.RETIREMENT_FI
    assert r.target_node_key == "retirement.earliest_safe_age"
    assert r.severity == "BLOCKER"


def test_route_finding_unrouted_subject_returns_none():
    assert route_finding(_f("")) is None
    assert route_finding(_f("mystery")) is None


def test_route_verdict_splits_routed_and_unroutable():
    verdict = WholeArtifactVerdict(overall_assessment="BLOCK", findings=[
        _f("retirement_age_headline"),
        _f("tranche_execution_gate"),
        _f("mystery"),                 # unroutable
        _f("net_worth_liquid", sev="YELLOW"),
    ])
    routed, unroutable = route_verdict(verdict)  # default: all severities
    owners = {r.owner for r in routed}
    assert OwnerRole.RETIREMENT_FI in owners and OwnerRole.INVESTMENT in owners
    assert OwnerRole.BALANCE_SHEET in owners
    assert len(routed) == 3 and len(unroutable) == 1
    assert unroutable[0].subject_type == "mystery"
    # severity filter
    routed_blockers, _ = route_verdict(verdict, severities=("BLOCKER",))
    assert all(r.severity == "BLOCKER" for r in routed_blockers)


def test_to_change_request_objection_for_figure_subject():
    r = route_finding(_f("net_worth_liquid"))
    cr = to_change_request(r)
    assert cr is not None
    assert cr.target_node_key == "portfolio.liquid_net_worth_nis"
    assert cr.kind is ChangeKind.OBJECTION
    assert cr.author.kind is AuthorKind.AGENT
    assert cr.author.role == "whole_artifact_reader"
    # a prose/policy subject (no target node) has no single-node change-request
    assert to_change_request(route_finding(_f("rsu_vest_policy"))) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finding_router.py -k "route_finding or route_verdict or change_request" -v`
Expected: FAIL — `ImportError: cannot import name 'RoutedFinding'`.

- [ ] **Step 3: Implement**

```python
# append to argosy/quality/finding_router.py
from argosy.quality.change_adjudication import Author, AuthorKind, ChangeKind, ChangeRequest

_READER_ROLE = "whole_artifact_reader"


@dataclass(frozen=True)
class RoutedFinding:
    """A reader finding routed to its accountable owner."""

    subject_type: str
    owner: OwnerRole
    target_node_key: str | None
    severity: str
    kind: str
    detail: str
    surfaces_cited: tuple[str, ...] = ()


def route_finding(finding) -> RoutedFinding | None:
    """Route one CoherenceFinding to its owner, or None when its subject_type is
    empty / unknown (unroutable — the caller surfaces it, never silently drops)."""
    subject = getattr(finding, "subject_type", "") or ""
    owner = subject_owner(subject)
    if owner is None:
        return None
    return RoutedFinding(
        subject_type=subject,
        owner=owner,
        target_node_key=subject_target_node(subject),
        severity=getattr(finding, "severity", ""),
        kind=getattr(finding, "kind", ""),
        detail=getattr(finding, "detail", ""),
        surfaces_cited=tuple(getattr(finding, "surfaces_cited", ()) or ()),
    )


def route_verdict(verdict, *, severities: tuple[str, ...] = ("BLOCKER", "AMBER", "YELLOW")):
    """Split a verdict's findings into (routed, unroutable). Only findings whose
    severity is in ``severities`` are considered; an in-scope finding with no
    routable subject is returned in ``unroutable`` (fail-loud, never dropped)."""
    routed: list[RoutedFinding] = []
    unroutable: list = []
    for f in getattr(verdict, "findings", []) or []:
        if getattr(f, "severity", "") not in severities:
            continue
        r = route_finding(f)
        if r is None:
            unroutable.append(f)
        else:
            routed.append(r)
    return routed, unroutable


def to_change_request(routed: "RoutedFinding | None") -> ChangeRequest | None:
    """Convert a routed FIGURE finding into a ChangeRequest(OBJECTION) against its
    target node — the artifact the Phase-3b cycle integration dispatches to the
    owner via the negotiation ladder. Returns None for a prose/policy subject
    (no single target node) — those route to the owner but aren't a one-node
    objection."""
    if routed is None or routed.target_node_key is None:
        return None
    return ChangeRequest(
        target_node_key=routed.target_node_key,
        author=Author(kind=AuthorKind.AGENT, role=_READER_ROLE),
        kind=ChangeKind.OBJECTION,
        payload={"severity": routed.severity, "finding_kind": routed.kind,
                 "surfaces_cited": list(routed.surfaces_cited)},
        rationale=routed.detail,
    )
```

Add the new public names to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finding_router.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add argosy/quality/finding_router.py tests/test_finding_router.py
git commit -m "feat(routing): route reader findings to owners + OBJECTION change-requests (Phase 3a)"
```

---

## Self-Review

**Spec coverage:** implements spec Phase-3 principle 4 ("Compliance routes findings to owners, never rewrites") + the Finding-schema routing — the deterministic spine. Owner truth has ONE source (figure_registry) for figure subjects; explicit fallback only for prose/policy subjects, asserted complete over `SUBJECT_REGISTRY`. The cycle integration (feed change-requests to `run_incremental_cycle`, extend OBJECTION→owner-remediation, dispatch `RealLadderParticipants`) is the Phase-3b follow-on — noted, not built here.

**Placeholder scan:** every step has complete code + commands + expected output.

**Type consistency:** `subject_owner`/`subject_target_node`/`route_finding`/`route_verdict`/`to_change_request`/`RoutedFinding` names identical across module + tests; `ChangeRequest`/`Author`/`AuthorKind`/`ChangeKind` match `change_adjudication`; `OwnerRole` matches `figure_registry`; subject keys match `SUBJECT_REGISTRY` + `CANONICAL_SUBJECT_NODE`.

**Risk:** pure routing, no mutation; the only judgment is the prose/policy fallback owners (EQUITY_COMP/INVESTMENT), which match the spec roster and the existing `ladder_participants._OWNER_BY_PREFIX` intent.
