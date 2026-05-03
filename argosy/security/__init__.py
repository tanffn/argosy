"""Argosy security package (Phase 5).

Phase 5 introduces TOTP-based second-factor for T3 approvals (SDD §10.2,
OPEN-8). The simpler "manual confirm + 1h delay" alternative is also
supported via `agent_settings.security.t3_second_factor = "delay"`.
"""

from argosy.security.totp import (
    TOTPVerificationError,
    generate_secret,
    provisioning_uri,
    verify_code,
)

__all__ = [
    "TOTPVerificationError",
    "generate_secret",
    "provisioning_uri",
    "verify_code",
]
