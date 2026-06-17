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


def verify_invariants(invariants: list[Invariant], artifact: dict[str, str]) -> VerifyResult:
    failures: list[str] = []
    for inv in invariants:
        failures.extend(inv.check(artifact))
    return VerifyResult(ok=not failures, failures=failures)
