"""Distinguishable SEC EDGAR failure classes (Stream A).

Misconfiguration, rate limits, and empty issuer data must never collapse into
one generic ``provenance_unknown``. Callers stamp enrichment sidecars and
gate ``blocked_by`` from these kinds.
"""

from __future__ import annotations

from enum import Enum


class SecFailureKind(str, Enum):
    CONTACT_EMAIL_UNSET = "sec_contact_email_unset"
    HTTP_403 = "sec_http_403"
    HTTP_429 = "sec_http_429"
    TIMEOUT = "sec_timeout"
    HTTP_OTHER = "sec_http_error"
    NO_CIK = "sec_no_cik"
    NO_REPORTED_PERIOD = "sec_no_reported_period"
    MALFORMED = "sec_malformed"


class SecProviderError(RuntimeError):
    """SEC EDGAR call failed — ``kind`` is the durable discriminator."""

    def __init__(self, kind: SecFailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class SecContactEmailUnsetError(SecProviderError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            SecFailureKind.CONTACT_EMAIL_UNSET,
            message
            or (
                "ARGOSY_SEC_CONTACT_EMAIL is unset or local; SEC EDGAR "
                "requires a declared contact email in the User-Agent"
            ),
        )


class SecHttpStatusError(SecProviderError):
    def __init__(self, status: int, detail: str = "") -> None:
        if status == 403:
            kind = SecFailureKind.HTTP_403
        elif status == 429:
            kind = SecFailureKind.HTTP_429
        else:
            kind = SecFailureKind.HTTP_OTHER
        msg = f"SEC EDGAR HTTP {status}"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(kind, msg)
        self.status = status


class SecTimeoutError(SecProviderError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            SecFailureKind.TIMEOUT,
            f"SEC EDGAR timeout{': ' + detail if detail else ''}",
        )


# Transient provider outages — vintage gate may fail OPEN with a loud,
# named exemption (see vintage_gate + STREAM_A outage policy).
SEC_OUTAGE_KINDS: frozenset[SecFailureKind] = frozenset(
    {
        SecFailureKind.HTTP_403,
        SecFailureKind.HTTP_429,
        SecFailureKind.TIMEOUT,
        SecFailureKind.HTTP_OTHER,
    }
)


__all__ = [
    "SEC_OUTAGE_KINDS",
    "SecContactEmailUnsetError",
    "SecFailureKind",
    "SecHttpStatusError",
    "SecProviderError",
    "SecTimeoutError",
]
