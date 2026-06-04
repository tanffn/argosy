"""Unit tests for the shared lenient JSON parser."""

from __future__ import annotations

import json

import pytest

from argosy.agents._json_parse import lenient_json_loads


def test_plain_json():
    assert lenient_json_loads('{"a": 1}') == {"a": 1}


def test_json_fence():
    assert lenient_json_loads('```json\n{"a": 1}\n```') == {"a": 1}


def test_bare_fence():
    assert lenient_json_loads('```\n{"a": 1}\n```') == {"a": 1}


def test_trailing_prose():
    assert lenient_json_loads('{"a": 1}\nThanks, hope this helps!') == {"a": 1}


def test_prose_preamble():
    assert lenient_json_loads('Here is the output:\n{"a": 1}') == {"a": 1}


def test_raw_control_chars_in_string():
    # A literal newline inside a string value (strict=False tolerates it).
    assert lenient_json_loads('{"a": "line1\nline2"}') == {"a": "line1\nline2"}


def test_array_value():
    assert lenient_json_loads('```json\n[1, 2, 3]\n```') == [1, 2, 3]


def test_unrecoverable_raises():
    with pytest.raises(json.JSONDecodeError):
        lenient_json_loads("this is not json at all")
