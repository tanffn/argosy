"""Email approval channel: token round-trip + send mocking."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from argosy.channels.email import (
    EmailApprovalLink,
    EmailSettings,
    send_approval_email,
)


# ----------------------------------------------------------------------
# Token primitives
# ----------------------------------------------------------------------


def test_issue_and_verify_round_trip() -> None:
    link = EmailApprovalLink(signing_key="test-key")
    token = link.issue(proposal_id=42, user_id="ariel", action="approve")
    payload = link.verify(token)
    assert payload is not None
    assert payload.proposal_id == 42
    assert payload.user_id == "ariel"
    assert payload.action == "approve"
    assert payload.exp > payload.iat


def test_verify_rejects_tampered_payload() -> None:
    link = EmailApprovalLink(signing_key="test-key")
    token = link.issue(proposal_id=42, user_id="ariel", action="approve")
    h, p, s = token.split(".")
    # Mutate payload: change "approve" → "reject" by re-issuing without signing
    other = EmailApprovalLink(signing_key="test-key").issue(
        proposal_id=42, user_id="ariel", action="reject"
    )
    _, p2, _ = other.split(".")
    forged = f"{h}.{p2}.{s}"  # original signature on different payload
    assert link.verify(forged) is None


def test_verify_rejects_wrong_signing_key() -> None:
    link_a = EmailApprovalLink(signing_key="key-a")
    link_b = EmailApprovalLink(signing_key="key-b")
    token = link_a.issue(proposal_id=1, user_id="ariel", action="approve")
    assert link_b.verify(token) is None


def test_verify_rejects_expired_token() -> None:
    link = EmailApprovalLink(signing_key="test-key")
    past = datetime.now(timezone.utc) - timedelta(hours=48)
    token = link.issue(
        proposal_id=1, user_id="ariel", action="approve", now=past, ttl_seconds=60
    )
    assert link.verify(token) is None


def test_verify_rejects_garbage_token() -> None:
    link = EmailApprovalLink(signing_key="test-key")
    assert link.verify("") is None
    assert link.verify("not.a.token") is None
    assert link.verify("a.b") is None


def test_verify_rejects_wrong_action_field() -> None:
    """An action other than approve|reject is invalid (defense in depth)."""
    import base64
    import hashlib
    import hmac
    import json

    link = EmailApprovalLink(signing_key="test-key")
    header = {"alg": "HS256", "typ": "argosy.approval"}
    payload = {
        "pid": 1,
        "uid": "ariel",
        "act": "delete",
        "nonce": "abc",
        "iat": 1700000000,
        "exp": 9999999999,
    }

    def b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    h = b64(json.dumps(header).encode())
    p = b64(json.dumps(payload).encode())
    sig = hmac.new(b"test-key", f"{h}.{p}".encode(), hashlib.sha256).digest()
    s = b64(sig)
    token = f"{h}.{p}.{s}"
    assert link.verify(token) is None


# ----------------------------------------------------------------------
# Send approval email (mocked aiosmtplib)
# ----------------------------------------------------------------------


class _StubProposal:
    def __init__(self) -> None:
        self.id = 7
        self.tier = "T2"
        self.action = "buy"
        self.ticker = "NVDA"
        self.size_shares_or_currency = 10
        self.rationale_summary = "Rebalance per plan delta."


@pytest.mark.asyncio
async def test_send_approval_email_calls_smtp_with_correct_args() -> None:
    captured: dict = {}

    async def fake_sender(**kwargs) -> None:
        captured.update(kwargs)

    settings = EmailSettings(
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="argosy",
        smtp_use_tls=True,
        sender="argosy@example.com",
        public_url="http://localhost:8000",
    )
    link = EmailApprovalLink(signing_key="test-key")
    subject, body = await send_approval_email(
        proposal=_StubProposal(),
        recipient="ariel@example.com",
        user_id="ariel",
        settings=settings,
        sender=fake_sender,
        link=link,
    )

    assert "Approve T2 proposal" in subject
    assert "BUY" in subject
    assert "NVDA" in subject
    assert captured["host"] == "smtp.example.com"
    assert captured["port"] == 465
    assert captured["recipient"] == "ariel@example.com"
    assert captured["sender"] == "argosy@example.com"
    assert "approve?token=" in body
    # Two distinct tokens in body (approve + reject)
    approve_count = body.count("approve?token=")
    assert approve_count == 2  # one for approve, one for reject (same path)


@pytest.mark.asyncio
async def test_email_links_verify_back_to_payload() -> None:
    """Tokens embedded in the email body must round-trip through verify()."""
    captured: dict = {}

    async def fake_sender(**kwargs) -> None:
        captured.update(kwargs)

    link = EmailApprovalLink(signing_key="rk")
    settings = EmailSettings(public_url="http://localhost:8000")
    _, body = await send_approval_email(
        proposal=_StubProposal(),
        recipient="r@x",
        user_id="ariel",
        settings=settings,
        sender=fake_sender,
        link=link,
    )

    # Extract first token between '?token=' and end-of-line
    pieces = [piece for piece in body.split() if piece.startswith("http")]
    assert len(pieces) == 2
    token = pieces[0].split("token=", 1)[1]
    payload = link.verify(token)
    assert payload is not None
    assert payload.proposal_id == 7
    assert payload.action == "approve"
