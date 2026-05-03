"""TOTP secret generation, verify, drift, and replay protection (Phase 5)."""

from __future__ import annotations

import time

import pytest

from argosy.security import totp as totp_mod
from argosy.state import db as db_mod
from argosy.state.models import TOTPSecret, User


def test_generate_secret_is_base32_and_long_enough() -> None:
    s = totp_mod.generate_secret()
    assert len(s) >= 32  # 20 bytes -> 32 base32 chars
    # Decodes cleanly
    raw = totp_mod._b32_decode_padded(s)
    assert len(raw) == 20


def test_verify_accepts_current_step() -> None:
    secret = totp_mod.generate_secret()
    code = totp_mod.generate_code(secret)
    result = totp_mod.verify_code(secret, code)
    assert result.counter == totp_mod.now_counter()


def test_verify_rejects_invalid_code() -> None:
    secret = totp_mod.generate_secret()
    with pytest.raises(totp_mod.TOTPVerificationError):
        totp_mod.verify_code(secret, "000000")


def test_verify_rejects_wrong_format() -> None:
    secret = totp_mod.generate_secret()
    with pytest.raises(totp_mod.TOTPVerificationError):
        totp_mod.verify_code(secret, "abc")
    with pytest.raises(totp_mod.TOTPVerificationError):
        totp_mod.verify_code(secret, "")


def test_verify_accepts_drift_window() -> None:
    """Accepts code from one step ago (clock drift)."""
    secret = totp_mod.generate_secret()
    now = time.time()
    past_code = totp_mod.generate_code(secret, at=now - 30)
    # Verify "now" with drift_steps=1 should accept past_code.
    result = totp_mod.verify_code(secret, past_code, at=now)
    assert result is not None


def test_verify_rejects_outside_drift_window() -> None:
    """Code from 5 steps ago is outside default drift window."""
    secret = totp_mod.generate_secret()
    now = time.time()
    old_code = totp_mod.generate_code(secret, at=now - 300)
    with pytest.raises(totp_mod.TOTPVerificationError):
        totp_mod.verify_code(secret, old_code, at=now)


def test_replay_protection_via_last_used_counter() -> None:
    secret = totp_mod.generate_secret()
    now = time.time()
    code = totp_mod.generate_code(secret, at=now)
    counter = totp_mod.now_counter(at=now)
    # First verify works
    result = totp_mod.verify_code(secret, code, at=now)
    assert result.counter == counter
    # Second verify with same counter as last_used → replay error
    with pytest.raises(totp_mod.TOTPVerificationError):
        totp_mod.verify_code(secret, code, at=now, last_used_counter=counter)


def test_provisioning_uri_contains_required_fields() -> None:
    secret = "JBSWY3DPEHPK3PXP"
    uri = totp_mod.provisioning_uri(secret=secret, account_name="ariel")
    assert uri.startswith("otpauth://totp/")
    assert "secret=" + secret in uri
    assert "issuer=Argosy" in uri


@pytest.mark.asyncio
async def test_set_and_get_user_secret(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    secret = totp_mod.generate_secret()
    await totp_mod.set_user_totp_secret("ariel", secret)
    fetched = await totp_mod.get_user_totp_secret("ariel")
    assert fetched == secret

    # Replace
    new = totp_mod.generate_secret()
    await totp_mod.set_user_totp_secret("ariel", new)
    fetched2 = await totp_mod.get_user_totp_secret("ariel")
    assert fetched2 == new


@pytest.mark.asyncio
async def test_mark_verified_advances_last_verified_at(engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()
    await totp_mod.set_user_totp_secret("ariel", totp_mod.generate_secret())
    target = 1_700_000_000.0
    await totp_mod.mark_verified("ariel", at=target)
    async with db_mod.get_session() as session:
        row = await session.get(TOTPSecret, "ariel")
        assert row.last_verified_at is not None
        # SQLite drops tzinfo; reattach UTC for stable epoch comparison.
        from datetime import timezone

        ts = row.last_verified_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        assert int(ts.timestamp()) == int(target)
