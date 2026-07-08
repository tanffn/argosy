"""Patch-reachability classifier for corrective patch-synthesis (part A).

Design: docs/design/corrective_patch_synthesis.md §2.B.

Maps each correction / directive from the corrective context onto the PRIOR
draft's structured artifact (``PlanSynthesisOutput``) and decides, with
strict FULL-first precedence, whether the run may take the phase-3 PATCH
path (per-slice edits, deterministically merged) or must take the shipped
full corrective regeneration.

Pure / deterministic — no DB, no LLM, no network. Same doctrine as
``argosy/quality/blast_radius.py``: the classifier decides *scope*; it never
judges whether a correction is *right* (the blind gates own that).

Slice model (design §2.A):

* Call/merge unit — the SLICE: one ``HorizonSection`` (``long`` / ``medium``
  / ``short``) or one ``Section`` (by ``(section_id, horizon)``).
* Spread accounting — the four COARSE groups ``long / medium / short /
  sections`` (matching ``_rewrite_output_parallel``'s slices).
* Honesty unit — the ITEM (synthetic ``<horizon>.<kind>.<slug>`` id, the
  same slug scheme as ``_pkg_build_prior_items_index``, or a Delta's own
  ``item_id``, or a Section key).

Per-correction FULL triggers, in design order (any one → the WHOLE run takes
the shipped full path; no mixed mode):

1. Unaddressable surface — ``plan_item_ref`` resolves to no item/section in
   the prior draft (lenient token match, ``findings_match`` spirit).
2. No concrete edit — neither canonical values nor wrong values nor a
   verbatim directive detail. Substance-only findings are cross-cutting
   judgment work; regeneration is the right tool.
3. Cross-cutting occurrence spread — the deterministic occurrence pre-scan
   (``corrections_check.value_variants`` + ``_present``) finds the
   wrong/canonical values in MORE than 2 of the 4 coarse slices, or the
   union of implicated slices across all corrections exceeds 2 of 4.
4. Status-class flip — the correction's concrete edit is a horizon
   ``status`` value (``no_change`` / ``minor_revision`` / ``major_revision``)
   or its ref addresses a horizon's ``status`` field. (Adding/removing an
   item class cannot be requested by a value-shaped correction; a ref to a
   not-yet-existing item/section already fails trigger 1.)
5. Snapshot-class correction — ``forces_full_tier`` on the corrective
   context, carried through unchanged (checked first because it is
   run-level, not per-correction).

Crucially, slice implication is occurrence-based, not just ref-based: every
slice (and every ITEM) where any wrong/canonical value textually occurs in
the prior artifact joins the implicated set — closing the "value restated in
another horizon's rationale" hole BEFORE the model runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from argosy.quality.corrections_check import _present, value_variants

if TYPE_CHECKING:  # pragma: no cover — typing only, keeps the module pure
    from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput

HORIZONS = ("long", "medium", "short")
SLICE_GROUPS = ("long", "medium", "short", "sections")
# > MAX_IMPLICATED_GROUPS of the 4 coarse groups → FULL (design trigger 3).
MAX_IMPLICATED_GROUPS = 2

ITEM_KINDS = ("targets", "themes", "actions", "speculative_candidates")
_KIND_ALIASES = {
    "target": "targets",
    "targets": "targets",
    "theme": "themes",
    "themes": "themes",
    "action": "actions",
    "actions": "actions",
    "speculative_candidate": "speculative_candidates",
    "speculative_candidates": "speculative_candidates",
}
_STATUS_LITERALS = frozenset({"no_change", "minor_revision", "major_revision"})
_TOKEN_RE = re.compile(r"[a-z0-9%]+")


def item_slug(label: str) -> str:
    """The synthetic-id slug — MUST mirror ``_pkg_build_prior_items_index``."""
    return "".join(
        c if c.isalnum() else "_" for c in (label or "").lower()
    ).strip("_")[:40]


def synthetic_item_id(horizon: str, kind: str, label: str) -> str:
    return f"{horizon}.{kind}.{item_slug(label)}"


def _tokens(text: Any) -> set[str]:
    return set(_TOKEN_RE.findall(str(text or "").lower()))


def _token_match(ref_tokens: set[str], subject_tokens: set[str]) -> bool:
    """Lenient matcher — ``findings_match`` spirit: >=50% overlap of the
    smaller token set."""
    if not ref_tokens or not subject_tokens:
        return False
    overlap = len(ref_tokens & subject_tokens)
    return overlap / min(len(ref_tokens), len(subject_tokens)) >= 0.5


def _occurrence_variants(value: Any) -> list[str]:
    """``value_variants`` plus the float-JSON form of integers, so an
    occurrence scan over ``model_dump_json`` text (where ``value: 4136``
    renders as ``4136.0``) still finds integer canonical/wrong values."""
    variants = list(value_variants(value))
    if isinstance(value, bool):
        return variants
    if isinstance(value, int):
        variants.append(f"{value}.0")
    if isinstance(value, float) and value == int(value):
        variants.append(f"{int(value)}.0")
    return variants


def _occurs(value: Any, text: str) -> bool:
    return any(_present(v, text) for v in _occurrence_variants(value))


# ---------------------------------------------------------------------------
# Parsed ref addressing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedRef:
    """Structured form of a ``plan_item_ref`` (``<horizon>.<kind>.<slug>`` /
    ``section:<id>`` addressing). ``group=None`` means unparseable — the
    resolver falls back to lenient token matching."""

    group: str | None = None          # long|medium|short|sections
    item_kind: str | None = None      # targets|themes|actions|speculative_candidates
    slug: str | None = None
    section_id: str | None = None
    horizon_field: str | None = None  # e.g. "status" / "posture" / "rationale"

    def to_payload(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "item_kind": self.item_kind,
            "slug": self.slug,
            "section_id": self.section_id,
            "horizon_field": self.horizon_field,
        }


def parse_plan_item_ref(ref: str) -> ParsedRef:
    """Deterministic parse of the corrections' addressing scheme."""
    r = (ref or "").strip()
    if not r:
        return ParsedRef()
    low = r.lower()
    if low.startswith("section:"):
        sid = r.split(":", 1)[1].strip()
        return ParsedRef(group="sections", section_id=sid or None)
    parts = low.split(".")
    if parts[0] in ("section", "sections") and len(parts) >= 2:
        return ParsedRef(group="sections", section_id=parts[1] or None)
    if parts[0] in HORIZONS:
        horizon = parts[0]
        if len(parts) == 1:
            return ParsedRef(group=horizon)
        kind = _KIND_ALIASES.get(parts[1])
        if kind is not None:
            slug = ".".join(parts[2:]) or None
            return ParsedRef(group=horizon, item_kind=kind, slug=slug)
        # Horizon-level field addressing ("long.status", "medium.posture").
        return ParsedRef(group=horizon, horizon_field=parts[1])
    return ParsedRef()


# ---------------------------------------------------------------------------
# Prior-artifact index (built once per classification)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ItemEntry:
    item_id: str
    group: str      # horizon
    kind: str       # targets|themes|actions|speculative_candidates|delta
    label: str
    text: str       # JSON dump — occurrence-scan haystack


@dataclass(frozen=True)
class _SectionEntry:
    section_id: str
    horizon: str
    title: str
    text: str


@dataclass
class _PriorIndex:
    items: list[_ItemEntry] = field(default_factory=list)
    sections: list[_SectionEntry] = field(default_factory=list)
    # Coarse-group haystacks for slice-level occurrence scanning.
    group_text: dict[str, str] = field(default_factory=dict)


def _build_prior_index(prior: PlanSynthesisOutput) -> _PriorIndex:
    idx = _PriorIndex()
    for horizon in HORIZONS:
        hz = getattr(prior, horizon)
        idx.group_text[horizon] = hz.model_dump_json()
        for kind in ITEM_KINDS:
            for entry in getattr(hz, kind):
                label = getattr(entry, "label", None) or getattr(
                    entry, "ticker", ""
                )
                idx.items.append(_ItemEntry(
                    item_id=synthetic_item_id(horizon, kind, label),
                    group=horizon,
                    kind=kind,
                    label=label,
                    text=entry.model_dump_json(),
                ))
        for delta in hz.deltas_from_prior:
            idx.items.append(_ItemEntry(
                item_id=delta.item_id,
                group=horizon,
                kind="delta",
                label=delta.summary or "",
                text=delta.model_dump_json(),
            ))
    section_texts: list[str] = []
    for s in prior.sections:
        text = s.model_dump_json()
        section_texts.append(text)
        idx.sections.append(_SectionEntry(
            section_id=s.section_id, horizon=s.horizon,
            title=s.title, text=text,
        ))
    idx.group_text["sections"] = "\n".join(section_texts)
    return idx


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass
class ScopeDecision:
    """Per-correction / per-directive scope verdict."""

    kind: str                # "correction" | "directive"
    index: int               # the correction/directive's own 1-based index
    scope: str               # "PATCH" | "FULL"
    reason: str
    implicated_groups: tuple[str, ...] = ()
    implicated_item_ids: tuple[str, ...] = ()
    # (section_id, horizon) keys implicated in the sections group.
    implicated_sections: tuple[tuple[str, str], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "index": self.index,
            "scope": self.scope,
            "reason": self.reason,
            "implicated_groups": list(self.implicated_groups),
            "implicated_item_ids": list(self.implicated_item_ids),
            "implicated_sections": [list(k) for k in self.implicated_sections],
        }


@dataclass
class PatchReachability:
    """Overall verdict + per-correction scopes + the union implication set."""

    verdict: str             # "PATCH" | "FULL_RESYNTH"
    reason: str
    decisions: list[ScopeDecision] = field(default_factory=list)
    implicated_groups: tuple[str, ...] = ()
    implicated_item_ids: tuple[str, ...] = ()
    implicated_sections: tuple[tuple[str, str], ...] = ()

    def corrections_for_horizon(self, horizon: str) -> list[ScopeDecision]:
        return [d for d in self.decisions if horizon in d.implicated_groups]

    def corrections_for_section(self, key: tuple[str, str]) -> list[ScopeDecision]:
        return [d for d in self.decisions if key in d.implicated_sections]

    def to_payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "decisions": [d.to_payload() for d in self.decisions],
            "implicated_groups": list(self.implicated_groups),
            "implicated_item_ids": list(self.implicated_item_ids),
            "implicated_sections": [list(k) for k in self.implicated_sections],
        }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _extract_values(payload: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """(canonical_values, wrong_values) from either payload dialect —
    ``Correction.to_payload`` (``canonical_facts`` pairs) or
    ``Correction.check_payload`` (``canonical_values`` flat list)."""
    canonical: list[Any] = []
    facts = payload.get("canonical_facts")
    if isinstance(facts, list):
        for pair in facts:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                canonical.append(pair[1])
    for v in payload.get("canonical_values") or []:
        if v is not None:
            canonical.append(v)
    wrong = [v for v in (payload.get("wrong_values") or []) if v is not None]
    return canonical, wrong


def _resolve_ref(
    ref: str, idx: _PriorIndex,
) -> tuple[set[str], set[str], set[tuple[str, str]], ParsedRef, bool]:
    """Resolve one ref against the prior index.

    Returns (groups, item_ids, section_keys, parsed, is_status_field).
    Empty groups == unresolved (potentially unaddressable).
    """
    parsed = parse_plan_item_ref(ref)
    groups: set[str] = set()
    item_ids: set[str] = set()
    section_keys: set[tuple[str, str]] = set()
    is_status = parsed.horizon_field == "status"

    if parsed.group in HORIZONS:
        if parsed.item_kind is not None and parsed.slug is not None:
            exact = f"{parsed.group}.{parsed.item_kind}.{parsed.slug}"
            hits = [
                it for it in idx.items
                if it.group == parsed.group and it.item_id == exact
            ]
            if not hits:
                ref_tokens = _tokens(parsed.slug.replace("_", " "))
                hits = [
                    it for it in idx.items
                    if it.group == parsed.group
                    and it.kind in (parsed.item_kind, "delta")
                    and (
                        _token_match(ref_tokens, _tokens(it.label))
                        or _token_match(
                            ref_tokens, _tokens(it.item_id.replace(".", " ").replace("_", " "))
                        )
                    )
                ]
            if hits:
                groups.add(parsed.group)
                item_ids.update(h.item_id for h in hits)
            # else: parsed but item not found → unresolved (caller decides).
        else:
            # Whole-horizon prose / field addressing resolves to the slice.
            groups.add(parsed.group)
        return groups, item_ids, section_keys, parsed, is_status

    if parsed.group == "sections":
        sid_tokens = _tokens((parsed.section_id or "").replace("_", " "))
        for s in idx.sections:
            if parsed.section_id and s.section_id == parsed.section_id:
                section_keys.add((s.section_id, s.horizon))
            elif sid_tokens and (
                _token_match(sid_tokens, _tokens(s.section_id.replace("_", " ")))
                or _token_match(sid_tokens, _tokens(s.title))
            ):
                section_keys.add((s.section_id, s.horizon))
        if section_keys:
            groups.add("sections")
        return groups, item_ids, section_keys, parsed, is_status

    # Unparseable ref — lenient token match over everything.
    ref_tokens = _tokens(ref.replace(".", " ").replace("_", " ").replace(":", " "))
    for it in idx.items:
        if _token_match(
            ref_tokens,
            _tokens(it.item_id.replace(".", " ").replace("_", " ")) | _tokens(it.label),
        ):
            groups.add(it.group)
            item_ids.add(it.item_id)
    for s in idx.sections:
        if _token_match(
            ref_tokens, _tokens(s.section_id.replace("_", " ")) | _tokens(s.title)
        ):
            groups.add("sections")
            section_keys.add((s.section_id, s.horizon))
    return groups, item_ids, section_keys, parsed, is_status


def _occurrence_implication(
    values: list[Any], idx: _PriorIndex,
) -> tuple[set[str], set[str], set[tuple[str, str]]]:
    """Every coarse group / item / section where any value textually occurs."""
    groups: set[str] = set()
    item_ids: set[str] = set()
    section_keys: set[tuple[str, str]] = set()
    for v in values:
        for group, text in idx.group_text.items():
            if _occurs(v, text):
                groups.add(group)
        for it in idx.items:
            if _occurs(v, it.text):
                item_ids.add(it.item_id)
        for s in idx.sections:
            if _occurs(v, s.text):
                section_keys.add((s.section_id, s.horizon))
    return groups, item_ids, section_keys


def _scope_one(
    *,
    kind: str,
    index: int,
    refs: list[str],
    canonical: list[Any],
    wrong: list[Any],
    has_concrete_edit: bool,
    idx: _PriorIndex,
    rendered_surfaces: dict[str, str] | None = None,
    global_surfaces: dict[str, str] | None = None,
) -> ScopeDecision:
    """Classify one correction/directive — strict FULL-first, design order."""
    groups: set[str] = set()
    item_ids: set[str] = set()
    section_keys: set[tuple[str, str]] = set()
    status_field_ref = False
    any_ref_given = any((r or "").strip() for r in refs)

    for r in refs:
        if not (r or "").strip():
            continue
        g, ii, sk, _parsed, is_status = _resolve_ref(r, idx)
        groups |= g
        item_ids |= ii
        section_keys |= sk
        status_field_ref = status_field_ref or is_status

    # 1 — unaddressable surface (strict FULL-first: a ref that resolves to
    # nothing forces FULL even when a value happens to occur somewhere —
    # the correction is asking for a surface the prior draft doesn't have).
    if any_ref_given and not groups:
        return ScopeDecision(
            kind=kind, index=index, scope="FULL",
            reason=(
                f"unaddressable surface: ref(s) {refs!r} resolve to no "
                "item/section in the prior draft"
            ),
        )

    # Occurrence widening (design: implication is occurrence-based, not just
    # ref-based) — every slice/item where a wrong/canonical value textually
    # occurs joins the implicated set, even if the ref points elsewhere.
    occ_groups, occ_items, occ_sections = _occurrence_implication(
        canonical + wrong, idx,
    )
    groups |= occ_groups
    item_ids |= occ_items
    section_keys |= occ_sections

    # Rendered-surface widening (codex patch-review blocker #3): the prior
    # plan's RENDERED horizon markdown can restate a value the structured
    # artifact doesn't carry verbatim (appendices, prose-loop edits). A
    # wrong/canonical value occurring in a horizon's rendered surface
    # implicates that slice too.
    for h, text in (rendered_surfaces or {}).items():
        if h in HORIZONS and text and any(
            _occurs(v, text) for v in canonical + wrong
        ):
            groups.add(h)

    # Render-only global surfaces (e.g. target_allocation_json): a WRONG
    # value surviving there cannot be attributed to one slice — the patch
    # cannot address it by slice, so FULL (conservative; canonical values
    # are excluded because they legitimately appear in derived surfaces).
    for name, text in (global_surfaces or {}).items():
        if text and any(_occurs(v, text) for v in wrong):
            return ScopeDecision(
                kind=kind, index=index, scope="FULL",
                reason=(
                    f"wrong value occurs in render-only surface {name!r} — "
                    "not attributable to a single slice"
                ),
                implicated_groups=tuple(sorted(groups)),
            )

    # 1 (no-ref form) — nothing addressed and nothing located by value.
    if not groups:
        return ScopeDecision(
            kind=kind, index=index, scope="FULL",
            reason=(
                "unaddressable surface: no plan_item_ref and no value "
                "occurrence in the prior draft"
            ),
        )

    # 2 — no concrete edit (substance-only).
    if not has_concrete_edit:
        return ScopeDecision(
            kind=kind, index=index, scope="FULL",
            reason=(
                "no concrete edit: neither canonical values nor wrong values "
                "nor a verbatim directive detail — substance-only corrections "
                "are cross-cutting judgment work"
            ),
            implicated_groups=tuple(sorted(groups)),
        )

    # 3 — cross-cutting occurrence spread (per-correction bound).
    if len(groups) > MAX_IMPLICATED_GROUPS:
        return ScopeDecision(
            kind=kind, index=index, scope="FULL",
            reason=(
                f"cross-cutting spread: implicates {len(groups)} of "
                f"{len(SLICE_GROUPS)} slices ({', '.join(sorted(groups))})"
            ),
            implicated_groups=tuple(sorted(groups)),
        )

    # 4 — status-class flip.
    status_value = any(
        isinstance(v, str) and v.strip().lower() in _STATUS_LITERALS
        for v in canonical + wrong
    )
    if status_value or status_field_ref:
        return ScopeDecision(
            kind=kind, index=index, scope="FULL",
            reason="status-class flip: correction targets a horizon status value",
            implicated_groups=tuple(sorted(groups)),
        )

    return ScopeDecision(
        kind=kind, index=index, scope="PATCH",
        reason="concrete, addressable, bounded-spread edit",
        implicated_groups=tuple(sorted(groups)),
        implicated_item_ids=tuple(sorted(item_ids)),
        implicated_sections=tuple(sorted(section_keys)),
    )


def classify_patch_reachability(
    *,
    corrections: list[dict[str, Any]],
    directives: list[dict[str, Any]],
    prior: PlanSynthesisOutput,
    forces_full_tier: bool = False,
    rendered_surfaces: dict[str, str] | None = None,
    global_surfaces: dict[str, str] | None = None,
) -> PatchReachability:
    """Classify the whole corrective run: PATCH vs FULL_RESYNTH.

    ``corrections`` / ``directives`` are the ``to_payload()`` dict shapes
    from ``argosy/services/corrective_context.py`` (no import edge back into
    services — this module stays pure). ``prior`` is the base document being
    edited (the prior CURRENT plan's structured artifact).

    ``rendered_surfaces`` (optional): per-horizon RENDERED markdown of the
    prior plan — occurrence hits widen that horizon's implication.
    ``global_surfaces`` (optional): render-only surfaces not attributable to
    one slice (e.g. ``target_allocation_json``) — a WRONG-value hit there
    forces FULL. Both stay plain strings so the module remains pure.
    """
    # 5 — snapshot-class correction: run-level, carried through unchanged.
    if forces_full_tier:
        return PatchReachability(
            verdict="FULL_RESYNTH",
            reason=(
                "snapshot-class correction forces the full tier "
                "(forces_full_tier carried through from the corrective context)"
            ),
        )

    if not corrections and not directives:
        return PatchReachability(
            verdict="FULL_RESYNTH",
            reason="no corrections or directives — nothing for a patch to clear",
        )

    idx = _build_prior_index(prior)
    decisions: list[ScopeDecision] = []

    for c in corrections:
        canonical, wrong = _extract_values(c)
        decisions.append(_scope_one(
            kind="correction",
            index=int(c.get("index") or 0),
            refs=[str(c.get("plan_item_ref") or "")],
            canonical=canonical,
            wrong=wrong,
            has_concrete_edit=bool(canonical or wrong),
            idx=idx,
            rendered_surfaces=rendered_surfaces,
            global_surfaces=global_surfaces,
        ))

    for d in directives:
        superseded = [
            v for v in (d.get("superseded_values") or []) if v is not None
        ]
        refs = [str(r) for r in (d.get("target_refs") or [])]
        decisions.append(_scope_one(
            kind="directive",
            index=int(d.get("index") or 0),
            refs=refs,
            canonical=[],
            wrong=superseded,
            # A directive's verbatim detail is a concrete edit (design
            # trigger 2) — but it still needs an addressable surface
            # (trigger 1 fires when refs + superseded values resolve to
            # nothing).
            has_concrete_edit=bool(
                (d.get("detail") or "").strip() or superseded
            ),
            idx=idx,
            rendered_surfaces=rendered_surfaces,
            global_surfaces=global_surfaces,
        ))

    full = [dec for dec in decisions if dec.scope == "FULL"]
    if full:
        first = full[0]
        return PatchReachability(
            verdict="FULL_RESYNTH",
            reason=f"[{first.kind} {first.index}] {first.reason}",
            decisions=decisions,
        )

    union_groups: set[str] = set()
    union_items: set[str] = set()
    union_sections: set[tuple[str, str]] = set()
    for dec in decisions:
        union_groups |= set(dec.implicated_groups)
        union_items |= set(dec.implicated_item_ids)
        union_sections |= set(dec.implicated_sections)

    # 3 (union clause) — most of the artifact implicated across corrections.
    if len(union_groups) > MAX_IMPLICATED_GROUPS:
        return PatchReachability(
            verdict="FULL_RESYNTH",
            reason=(
                f"cross-cutting spread: union of implicated slices is "
                f"{len(union_groups)} of {len(SLICE_GROUPS)} "
                f"({', '.join(sorted(union_groups))})"
            ),
            decisions=decisions,
        )

    return PatchReachability(
        verdict="PATCH",
        reason=(
            "every correction/directive is a concrete, addressable, "
            f"bounded-spread edit ({', '.join(sorted(union_groups))})"
        ),
        decisions=decisions,
        implicated_groups=tuple(sorted(union_groups)),
        implicated_item_ids=tuple(sorted(union_items)),
        implicated_sections=tuple(sorted(union_sections)),
    )


__all__ = [
    "HORIZONS",
    "ITEM_KINDS",
    "MAX_IMPLICATED_GROUPS",
    "ParsedRef",
    "PatchReachability",
    "SLICE_GROUPS",
    "ScopeDecision",
    "classify_patch_reachability",
    "item_slug",
    "parse_plan_item_ref",
    "synthetic_item_id",
]
