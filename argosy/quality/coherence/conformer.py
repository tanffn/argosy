# argosy/quality/coherence/conformer.py
"""Apply a typed patch plan across ALL surfaces atomically. Builds the full new
state first; if any patch is unsafe or unapplicable, returns ok=False with NO
mutation (callers must BLOCK). Number-boundary guard rejects a replacement that
introduces a numeric token not present in `find` or `allowed_numbers`. Markdown
patches are idempotent (a find already absent is a satisfied no-op)."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

_NUM = re.compile(r"\d[\d,\.]*")


@dataclass(frozen=True)
class ConformPatch:
    surface_id: str
    conform_method: str                 # "markdown" | "json_field"
    # markdown:
    find: str = ""
    replace: str = ""
    # json_field:
    match_label: str = ""               # substring match on actions[].label
    set_field: str = ""                 # e.g. "detail" | "label"
    new_value: str = ""


@dataclass
class ConformResult:
    ok: bool
    bodies: dict[str, str] = field(default_factory=dict)
    json_surfaces: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _numbers(text: str) -> set[str]:
    return {m.group(0).rstrip(".").replace(",", "") for m in _NUM.finditer(text)}


def _edit_introduces_number(find: str, replace: str, allowed: frozenset[str]) -> bool:
    introduced = _numbers(replace) - _numbers(find) - {n.replace(",", "") for n in allowed}
    return bool(introduced)


def apply_patches(
    bodies: dict[str, str],
    json_surfaces: dict[str, Any],
    patches: list[ConformPatch],
    *,
    allowed_numbers: frozenset[str] = frozenset(),
) -> ConformResult:
    new_bodies = dict(bodies)
    new_json = copy.deepcopy(json_surfaces)
    errors: list[str] = []

    for p in patches:
        if p.conform_method == "markdown":
            text = new_bodies.get(p.surface_id, "")
            if p.replace and _edit_introduces_number(p.find, p.replace, allowed_numbers):
                errors.append(f"{p.surface_id}: replacement introduces a fabricated number")
                continue
            if p.find and p.find in text:
                new_bodies[p.surface_id] = text.replace(p.find, p.replace, 1)
            elif p.find and p.replace and p.replace in text:
                pass  # idempotent: already conformed
            elif p.find:
                errors.append(f"{p.surface_id}: find text not present and not already conformed")
        elif p.conform_method == "json_field":
            surface = new_json.get(p.surface_id)
            if not isinstance(surface, dict):
                errors.append(f"{p.surface_id}: json surface missing")
                continue
            hits = [a for a in surface.get("actions") or []
                    if isinstance(a, dict) and p.match_label in (a.get("label") or "")]
            if len(hits) != 1:
                already = [a for a in surface.get("actions") or []
                           if isinstance(a, dict) and p.new_value in (a.get(p.set_field) or "")]
                if not already:
                    errors.append(f"{p.surface_id}: expected 1 action for '{p.match_label}', got {len(hits)}")
                continue
            hits[0][p.set_field] = p.new_value
        else:
            errors.append(f"{p.surface_id}: unknown conform_method {p.conform_method}")

    if errors:
        return ConformResult(ok=False, bodies=dict(bodies),
                             json_surfaces=copy.deepcopy(json_surfaces), errors=errors)
    return ConformResult(ok=True, bodies=new_bodies, json_surfaces=new_json)
