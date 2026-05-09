"""FX module exceptions."""

from __future__ import annotations


class FXRateUnavailable(Exception):
    """Raised when no rate can be found for the requested (date, currency) pair,
    after cache lookup, walkback, and any optional online fetch.
    """
