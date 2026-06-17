# argosy/quality/coherence/invariants.py
"""Typed, code-evaluable invariants over named surfaces. The verifier gates
MECHANICAL compliance only (numbers/fields/required+forbidden typed claims/
coverage). It never attempts semantic-truth checking of prose — that is the
reader-appeal layer's job. Framing-contract invariants are added in Slice 2.

`artifact` is a dict[surface_id -> str] (markdown bodies) plus, for json_field
surfaces, the verifier is given parsed claim text by the conformer (Slice 2 adds
claim markers; Slice 1 value checks operate on rendered text)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from argosy.quality.coherence.claim_markers import parse_markers


@dataclass
class VerifyResult:
    ok: bool
    failures: list[str] = field(default_factory=list)


class Invariant(Protocol):
    def check(self, artifact: dict[str, str]) -> list[str]:
        """Return a list of failure messages (empty == satisfied)."""
        ...


@dataclass(frozen=True)
class EqualsCanonical:
    subject_type: str
    canonical_text: str
    surfaces: tuple[str, ...]

    def check(self, artifact: dict[str, str]) -> list[str]:
        out: list[str] = []
        for s in self.surfaces:
            text = artifact.get(s)
            if text is None:
                out.append(f"{self.subject_type}: surface {s} absent")
            elif self.canonical_text not in text:
                out.append(
                    f"{self.subject_type}: surface {s} does not state "
                    f"canonical '{self.canonical_text}'"
                )
        return out


@dataclass(frozen=True)
class AllRegisteredSurfacesPresent:
    subject_type: str
    surfaces: tuple[str, ...]

    def check(self, artifact: dict[str, str]) -> list[str]:
        return [
            f"{self.subject_type}: registered surface {s} missing"
            for s in self.surfaces
            if artifact.get(s) is None
        ]


@dataclass(frozen=True)
class RequiredFramingRole:
    """A surface's typed claim marker must carry role_field == value."""
    subject_type: str
    surface: str
    role_field: str
    value: str

    def check(self, artifact: dict[str, str]) -> list[str]:
        text = artifact.get(self.surface)
        if text is None:
            return [f"{self.subject_type}: surface {self.surface} absent"]
        claims = parse_markers(text).get(self.subject_type, {})
        actual = claims.get(self.role_field)
        if actual != self.value:
            return [
                f"{self.subject_type}: {self.surface} framing {self.role_field}="
                f"{actual!r}, expected {self.value!r}"
            ]
        return []


@dataclass(frozen=True)
class ForbiddenClaim:
    """A surface's PROSE must not contain a forbidden substring (mechanical guard
    against a known-wrong claim, e.g. the retired 'retain as NVDA' policy)."""
    subject_type: str
    surface: str
    pattern: str

    def check(self, artifact: dict[str, str]) -> list[str]:
        text = artifact.get(self.surface) or ""
        if self.pattern in text:
            return [f"{self.subject_type}: {self.surface} contains forbidden claim '{self.pattern}'"]
        return []


@dataclass(frozen=True)
class SurfaceClaimEquals:
    """A typed claim block on a surface holds the expected value for claim_key."""
    subject_type: str
    surface: str
    claim_key: str
    value: str

    def check(self, artifact: dict[str, str]) -> list[str]:
        claims = parse_markers(artifact.get(self.surface) or "").get(self.subject_type, {})
        if claims.get(self.claim_key) != self.value:
            return [
                f"{self.subject_type}: {self.surface} claim {self.claim_key}="
                f"{claims.get(self.claim_key)!r}, expected {self.value!r}"
            ]
        return []


def verify_invariants(invariants: list[Invariant], artifact: dict[str, str]) -> VerifyResult:
    failures: list[str] = []
    for inv in invariants:
        failures.extend(inv.check(artifact))
    return VerifyResult(ok=not failures, failures=failures)
