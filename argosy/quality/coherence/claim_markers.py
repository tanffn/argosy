# argosy/quality/coherence/claim_markers.py
"""Typed claim markers: a machine-readable claim block embedded in markdown as an
HTML comment so it is invisible in rendered prose but deterministically parseable.
The verifier reads markers, never the prose. Markers are stripped from the
reader-facing artifact AND can be stripped for a clean human read.

Marker form:  <!--coh:subject_type k1=v1;k2=v2-->
"""
from __future__ import annotations

import re

_MARKER = re.compile(r"<!--coh:(?P<subj>[a-z0-9_]+)\s+(?P<body>[^>]*?)-->")


def render_marker(subject_type: str, claims: dict[str, str]) -> str:
    body = ";".join(f"{k}={v}" for k, v in claims.items())
    return f"<!--coh:{subject_type} {body}-->"


def parse_markers(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for m in _MARKER.finditer(text or ""):
        claims: dict[str, str] = {}
        for pair in m.group("body").split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                claims[k.strip()] = v.strip()
        out[m.group("subj")] = claims
    return out


def strip_markers(text: str) -> str:
    return _MARKER.sub("", text or "").rstrip()
