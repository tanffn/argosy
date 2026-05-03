"""TOTP secret-key generator + verifier (RFC 6238) for Phase 5.

Self-contained implementation to avoid the `pyotp` dependency. The
algorithm is short, well-specified, and easy to audit:

  - Secret is base32-encoded random bytes (default 20 bytes, 160 bits).
  - 30-second time step, 6-digit code, HMAC-SHA1 (RFC 6238 defaults).
  - Verify accepts a small +/- step window (default 1 step) for clock drift.
  - Replay protection: callers track `last_verified_at` (advanced past the
    counter step that succeeded) so the same code cannot reuse within its
    valid window.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import urllib.parse
from dataclasses import dataclass
from typing import Iterable


DEFAULT_STEP_SECONDS = 30
DEFAULT_DIGITS = 6
DEFAULT_DRIFT_STEPS = 1


class TOTPVerificationError(ValueError):
    """Raised when the user-supplied code does not match in the drift window."""


def generate_secret(num_bytes: int = 20) -> str:
    """Return a base32-encoded random secret. 160 bits is the RFC default."""
    raw = secrets.token_bytes(num_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _b32_decode_padded(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    pad = (-len(s)) % 8
    return base64.b32decode(s + "=" * pad)


def _hotp(secret: str, counter: int, *, digits: int = DEFAULT_DIGITS) -> str:
    """RFC 4226 HMAC-SHA1 HOTP."""
    key = _b32_decode_padded(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_bytes = digest[offset : offset + 4]
    code_int = struct.unpack(">I", code_bytes)[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


def now_counter(*, step_seconds: int = DEFAULT_STEP_SECONDS, at: float | None = None) -> int:
    moment = at if at is not None else time.time()
    return int(moment // step_seconds)


def generate_code(
    secret: str,
    *,
    at: float | None = None,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    digits: int = DEFAULT_DIGITS,
) -> str:
    return _hotp(secret, now_counter(step_seconds=step_seconds, at=at), digits=digits)


def candidate_counters(
    *,
    at: float | None = None,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    drift_steps: int = DEFAULT_DRIFT_STEPS,
) -> Iterable[int]:
    base = now_counter(step_seconds=step_seconds, at=at)
    return range(base - drift_steps, base + drift_steps + 1)


@dataclass
class VerifyResult:
    counter: int  # the HOTP counter that matched
    counter_at: float  # epoch time corresponding to that counter step


def verify_code(
    secret: str,
    code: str,
    *,
    at: float | None = None,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    digits: int = DEFAULT_DIGITS,
    drift_steps: int = DEFAULT_DRIFT_STEPS,
    last_used_counter: int | None = None,
) -> VerifyResult:
    """Verify `code` against `secret`.

    Raises `TOTPVerificationError` on:
      - wrong format
      - no counter match within the drift window
      - replay (counter <= last_used_counter)

    Returns the matched counter on success so callers can persist it as
    `last_used_counter` for replay protection.
    """
    if not code or not code.strip():
        raise TOTPVerificationError("empty code")
    code = code.strip()
    if not code.isdigit() or len(code) != digits:
        raise TOTPVerificationError(
            f"code must be {digits} digits; got {code!r}"
        )

    for counter in candidate_counters(
        at=at, step_seconds=step_seconds, drift_steps=drift_steps
    ):
        expected = _hotp(secret, counter, digits=digits)
        if hmac.compare_digest(expected, code):
            if last_used_counter is not None and counter <= last_used_counter:
                raise TOTPVerificationError(
                    "code replayed; this counter step has already been used"
                )
            return VerifyResult(counter=counter, counter_at=counter * step_seconds)

    raise TOTPVerificationError("invalid TOTP code")


def provisioning_uri(
    *,
    secret: str,
    account_name: str,
    issuer: str = "Argosy",
    digits: int = DEFAULT_DIGITS,
    step_seconds: int = DEFAULT_STEP_SECONDS,
) -> str:
    """Return an `otpauth://totp/...` URI for QR-code provisioning.

    Compatible with Authy / Google Authenticator / 1Password.
    """
    label = urllib.parse.quote(f"{issuer}:{account_name}")
    params = {
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": str(digits),
        "period": str(step_seconds),
    }
    qs = urllib.parse.urlencode(params)
    return f"otpauth://totp/{label}?{qs}"


# ----------------------------------------------------------------------
# DB-backed secret store
# ----------------------------------------------------------------------


def _fernet() -> Any:
    """Return a Fernet instance keyed off the OS keychain master key.

    The master key is created once on first use and stored in the keychain
    via `argosy.secrets`. If `cryptography` is unavailable or keychain
    access fails, raises a clear error so callers don't silently store
    plaintext.
    """
    import base64
    import hashlib

    from cryptography.fernet import Fernet  # type: ignore[import-not-found]

    from argosy.secrets import get_secret, set_secret

    key_name = "argosy.totp.master_key"
    raw = get_secret(key_name)
    if not raw:
        # Generate a fresh 256-bit key, base64-encoded for Fernet.
        new_key = Fernet.generate_key().decode("ascii")
        set_secret(key_name, new_key)
        raw = new_key
    # Fernet expects 32 url-safe-base64 bytes; if the keychain returned a
    # raw passphrase (legacy), derive a deterministic key via SHA-256.
    try:
        return Fernet(raw.encode("ascii"))
    except Exception:
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        return Fernet(derived)


def _encrypt(plaintext: str) -> str:
    """Encrypt a TOTP secret with Fernet; returns the base64 token as str."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str:
    """Decrypt a Fernet token; returns the plaintext secret."""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


async def set_user_totp_secret(user_id: str, secret: str) -> None:
    """Store (or replace) a user's TOTP secret in the DB.

    The secret is Fernet-encrypted at rest using a master key from the
    OS keychain. The keychain key is auto-created on first use. The DB
    column stores the Fernet ciphertext; plaintext never lands on disk.
    """
    from datetime import datetime, timezone

    from argosy.state import db as db_mod
    from argosy.state.models import TOTPSecret

    ciphertext = _encrypt(secret)
    async with db_mod.get_session() as session:
        existing = await session.get(TOTPSecret, user_id)
        if existing is None:
            session.add(
                TOTPSecret(
                    user_id=user_id,
                    secret_encrypted=ciphertext,
                    created_at=datetime.now(timezone.utc),
                )
            )
        else:
            existing.secret_encrypted = ciphertext
            existing.created_at = datetime.now(timezone.utc)
            existing.last_verified_at = None
        await session.commit()


async def get_user_totp_secret(user_id: str) -> str | None:
    """Return the user's stored TOTP secret (decrypted), or None."""
    from argosy.state import db as db_mod
    from argosy.state.models import TOTPSecret

    async with db_mod.get_session() as session:
        row = await session.get(TOTPSecret, user_id)
        if row is None or not row.secret_encrypted:
            return None
        try:
            return _decrypt(row.secret_encrypted)
        except Exception:
            # Legacy plaintext rows (pre-encryption fix): return as-is.
            # Will be re-encrypted on next set_user_totp_secret call.
            return row.secret_encrypted


async def mark_verified(
    user_id: str, *, at: float | None = None
) -> None:
    """Advance `last_verified_at` to `at` (epoch seconds, default now)."""
    from datetime import datetime, timezone

    from argosy.state import db as db_mod
    from argosy.state.models import TOTPSecret

    moment = datetime.fromtimestamp(at if at is not None else time.time(), tz=timezone.utc)
    async with db_mod.get_session() as session:
        row = await session.get(TOTPSecret, user_id)
        if row is None:
            return
        row.last_verified_at = moment
        await session.commit()


__all__ = [
    "DEFAULT_DIGITS",
    "DEFAULT_DRIFT_STEPS",
    "DEFAULT_STEP_SECONDS",
    "TOTPVerificationError",
    "VerifyResult",
    "candidate_counters",
    "generate_code",
    "generate_secret",
    "get_user_totp_secret",
    "mark_verified",
    "now_counter",
    "provisioning_uri",
    "set_user_totp_secret",
    "verify_code",
]


