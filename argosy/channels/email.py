"""Email approval channel (SDD §10.2, Phase 4).

Generates signed JWT-style tokens for proposal approve/reject with 24h
expiry. Sends them via SMTP using `aiosmtplib`. Token verification is
the *only* thing the email-link endpoint does — never one-click-approve
from the email itself (phishing surface per SDD §10.2). The email link
redirects to the dashboard where the user clicks to confirm.

Token format (compact JWS-like, HS256):

    base64url(header).base64url(payload).base64url(hmac)

  header  = {"alg":"HS256","typ":"argosy.approval"}
  payload = {
      "pid": <proposal_id>,
      "uid": <user_id>,
      "act": "approve" | "reject",
      "nonce": <hex>,
      "iat": <unix_ts>,
      "exp": <unix_ts + 24h>
  }

We don't depend on PyJWT to avoid an extra dep; HMAC-SHA256 is in stdlib.
The signing key is stored in the OS keychain under
`argosy.email.signing_key` and auto-generated on first use.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as stdsecrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

import yaml

from argosy.config import get_settings
from argosy.logging import get_logger
from argosy.secrets import get_secret, set_secret

_log = get_logger("argosy.channels.email")


SIGNING_KEY_NAME = "argosy.email.signing_key"
SMTP_PASSWORD_KEY_NAME = "argosy.email.smtp_password"
TOKEN_TTL_SECONDS = 24 * 60 * 60  # 24h
TOKEN_TYPE = "argosy.approval"


# ----------------------------------------------------------------------
# Token primitives
# ----------------------------------------------------------------------


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _ensure_signing_key() -> str:
    """Return the signing key, generating + storing it on first use."""
    key = get_secret(SIGNING_KEY_NAME)
    if key:
        return key
    new_key = stdsecrets.token_urlsafe(48)
    try:
        set_secret(SIGNING_KEY_NAME, new_key)
    except Exception:  # pragma: no cover - keyring backend missing
        _log.warning("email.signing_key.set_failed", entry=SIGNING_KEY_NAME)
    return new_key


@dataclass
class TokenPayload:
    proposal_id: int
    user_id: str
    action: Literal["approve", "reject"]
    nonce: str
    iat: int
    exp: int


class EmailApprovalLink:
    """Signed-token generator + verifier."""

    def __init__(self, signing_key: str | None = None) -> None:
        self._signing_key = signing_key or _ensure_signing_key()

    def issue(
        self,
        *,
        proposal_id: int,
        user_id: str,
        action: Literal["approve", "reject"],
        now: datetime | None = None,
        ttl_seconds: int = TOKEN_TTL_SECONDS,
    ) -> str:
        moment = now or datetime.now(timezone.utc)
        iat = int(moment.timestamp())
        exp = iat + ttl_seconds
        payload = {
            "pid": int(proposal_id),
            "uid": str(user_id),
            "act": action,
            "nonce": stdsecrets.token_hex(8),
            "iat": iat,
            "exp": exp,
        }
        header = {"alg": "HS256", "typ": TOKEN_TYPE}
        h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        sig = hmac.new(
            self._signing_key.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        s_b64 = _b64url_encode(sig)
        return f"{h_b64}.{p_b64}.{s_b64}"

    def verify(self, token: str, *, now: datetime | None = None) -> TokenPayload | None:
        if not token or token.count(".") != 2:
            return None
        h_b64, p_b64, s_b64 = token.split(".")
        try:
            header = json.loads(_b64url_decode(h_b64))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(header, dict) or header.get("typ") != TOKEN_TYPE:
            return None
        if header.get("alg") != "HS256":
            return None

        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        expected = hmac.new(
            self._signing_key.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        try:
            actual = _b64url_decode(s_b64)
        except ValueError:
            return None
        if not hmac.compare_digest(expected, actual):
            return None

        try:
            payload = json.loads(_b64url_decode(p_b64))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            tp = TokenPayload(
                proposal_id=int(payload["pid"]),
                user_id=str(payload["uid"]),
                action=str(payload["act"]),  # type: ignore[arg-type]
                nonce=str(payload["nonce"]),
                iat=int(payload["iat"]),
                exp=int(payload["exp"]),
            )
        except (KeyError, ValueError, TypeError):
            return None
        if tp.action not in ("approve", "reject"):
            return None

        moment = now or datetime.now(timezone.utc)
        if tp.exp < int(moment.timestamp()):
            return None
        return tp


# ----------------------------------------------------------------------
# SMTP send
# ----------------------------------------------------------------------


@dataclass
class EmailSettings:
    """Loaded from `configs/<user_id>/email_settings.yaml`."""

    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_use_tls: bool = True
    sender: str = "argosy@localhost"
    public_url: str = "http://localhost:8000"

    @classmethod
    def load(cls, user_id: str) -> "EmailSettings":
        settings = get_settings()
        path = settings.configs_dir / user_id / "email_settings.yaml"
        if not path.is_file():
            return cls()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:  # pragma: no cover - defensive
            return cls()
        return cls(
            smtp_host=data.get("smtp_host", "localhost"),
            smtp_port=int(data.get("smtp_port", 587)),
            smtp_username=data.get("smtp_username", ""),
            smtp_use_tls=bool(data.get("smtp_use_tls", True)),
            sender=data.get("sender", "argosy@localhost"),
            public_url=data.get("public_url", "http://localhost:8000"),
        )


# Tests inject a sender to bypass real SMTP. Type:
#   async fn(host, port, *, username, password, use_tls, sender, recipient,
#            subject, body) -> None
SmtpSender = Callable[..., Awaitable[None]]


async def _aiosmtplib_sender(**kwargs: Any) -> None:  # pragma: no cover - real SMTP
    """Real-SMTP send via aiosmtplib. Skipped in tests."""
    import aiosmtplib

    msg_lines = [
        f"From: {kwargs['sender']}",
        f"To: {kwargs['recipient']}",
        f"Subject: {kwargs['subject']}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        kwargs["body"],
    ]
    raw = "\r\n".join(msg_lines).encode("utf-8")

    await aiosmtplib.send(
        raw,
        hostname=kwargs["host"],
        port=kwargs["port"],
        username=kwargs.get("username") or None,
        password=kwargs.get("password") or None,
        use_tls=kwargs.get("use_tls", True),
        sender=kwargs["sender"],
        recipients=[kwargs["recipient"]],
    )


async def send_approval_email(
    *,
    proposal: Any,
    recipient: str,
    user_id: str,
    settings: EmailSettings | None = None,
    sender: SmtpSender | None = None,
    link: EmailApprovalLink | None = None,
) -> tuple[str, str]:
    """Compose + send an approval email. Returns (subject, body) for audit.

    `proposal` is duck-typed: must expose `id`, `tier`, `action`, `ticker`,
    `size_shares_or_currency`, `rationale_summary`. Both ORM `Proposal`
    rows and `decisions.proposals.Proposal` pydantic models satisfy this.
    """
    settings = settings or EmailSettings.load(user_id)
    link = link or EmailApprovalLink()
    smtp_sender = sender or _aiosmtplib_sender
    smtp_password = get_secret(SMTP_PASSWORD_KEY_NAME) or ""

    approve_token = link.issue(
        proposal_id=int(proposal.id),
        user_id=user_id,
        action="approve",
    )
    reject_token = link.issue(
        proposal_id=int(proposal.id),
        user_id=user_id,
        action="reject",
    )

    base = settings.public_url.rstrip("/")
    approve_url = f"{base}/api/proposals/{int(proposal.id)}/approve?token={approve_token}"
    reject_url = f"{base}/api/proposals/{int(proposal.id)}/approve?token={reject_token}"

    subject = (
        f"[Argosy] Approve {proposal.tier} proposal: "
        f"{str(proposal.action).upper()} {proposal.ticker}"
    )
    body = (
        f"Argosy proposal #{int(proposal.id)} ({proposal.tier})\n"
        f"\n"
        f"  Action:   {proposal.action} {proposal.ticker}\n"
        f"  Size:     {proposal.size_shares_or_currency}\n"
        f"  Rationale: {proposal.rationale_summary}\n"
        f"\n"
        f"Approve:  {approve_url}\n"
        f"Reject:   {reject_url}\n"
        f"\n"
        f"Both links open the dashboard for a one-click confirm; clicking "
        f"the email link by itself does not place the trade. Tokens expire "
        f"in 24 hours.\n"
    )

    await smtp_sender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=smtp_password,
        use_tls=settings.smtp_use_tls,
        sender=settings.sender,
        recipient=recipient,
        subject=subject,
        body=body,
    )

    return subject, body


__all__ = [
    "EmailApprovalLink",
    "EmailSettings",
    "TokenPayload",
    "TOKEN_TTL_SECONDS",
    "send_approval_email",
]
