# tests/coherence/test_conformer.py
import json
from argosy.quality.coherence.conformer import ConformPatch, apply_patches, ConformResult


def test_markdown_patch_applies_and_is_idempotent():
    bodies = {"long_md": "retain net vested as NVDA", "medium_md": "", "short_md": ""}
    patch = ConformPatch(
        surface_id="long_md", conform_method="markdown",
        find="retain net vested as NVDA", replace="sell net vested NVDA -> SGOV",
    )
    res = apply_patches(bodies, {}, [patch])
    assert res.ok
    assert "sell net vested NVDA -> SGOV" in res.bodies["long_md"]
    res2 = apply_patches(res.bodies, {}, [patch])
    assert res2.ok
    assert res2.bodies["long_md"] == res.bodies["long_md"]


def test_json_field_patch_sets_action_detail():
    actions = {"actions": [{"label": "UCITS dollar-cost tranche",
                            "detail": "split across CSPX/FUSA/EIMI/SGLN"}]}
    patch = ConformPatch(
        surface_id="short_actions_json", conform_method="json_field",
        match_label="UCITS dollar-cost", set_field="detail",
        new_value="split across CSPX/FUSA/EIMI only; SGLN standalone",
    )
    res = apply_patches({}, {"short_actions_json": actions}, [patch])
    assert res.ok
    assert "SGLN standalone" in res.json_surfaces["short_actions_json"]["actions"][0]["detail"]


def test_number_boundary_guard_rejects_fabricated_number():
    bodies = {"long_md": "earliest-safe age 46", "medium_md": "", "short_md": ""}
    patch = ConformPatch(
        surface_id="long_md", conform_method="markdown",
        find="earliest-safe age 46", replace="earliest-safe age 51",
    )
    res = apply_patches(bodies, {}, [patch], allowed_numbers=frozenset({"46", "54", "44"}))
    assert res.ok is False
    assert res.bodies == bodies
