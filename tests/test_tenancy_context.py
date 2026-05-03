"""Phase 6: tenancy contextvar + cross-tenant guards."""

from __future__ import annotations

import pytest

from argosy.tenancy.context import (
    CrossTenantAccessError,
    bind_user_id,
    current_user_id,
    require_user_id,
    set_current_user_id,
)


def test_current_user_id_default_none() -> None:
    # No prior bind in this test fn.
    assert current_user_id() is None


def test_bind_user_id_context_manager() -> None:
    assert current_user_id() is None
    with bind_user_id("alice"):
        assert current_user_id() == "alice"
    assert current_user_id() is None


def test_set_then_reset() -> None:
    tok = set_current_user_id("bob")
    try:
        assert current_user_id() == "bob"
    finally:
        # Use tok to reset
        from argosy.tenancy.context import _USER_ID  # noqa: PLC2701 - test access

        _USER_ID.reset(tok)
    assert current_user_id() is None


def test_require_user_id_uses_bound_when_no_claim() -> None:
    with bind_user_id("alice"):
        assert require_user_id() == "alice"


def test_require_user_id_accepts_matching_claim() -> None:
    with bind_user_id("alice"):
        assert require_user_id("alice") == "alice"


def test_require_user_id_rejects_mismatched_claim() -> None:
    with bind_user_id("alice"):
        with pytest.raises(CrossTenantAccessError):
            require_user_id("bob")


def test_require_user_id_raises_when_unbound_and_unclaimed() -> None:
    with pytest.raises(CrossTenantAccessError):
        require_user_id()
