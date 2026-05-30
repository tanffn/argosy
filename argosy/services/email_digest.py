"""Weekly email digest service — Spec E commit #8.

Composes, renders, and dispatches a weekly recap of Argosy activity
(monitor flags, action proposals, state-snapshot deltas).  The digest
is rendered from Jinja templates (``argosy/templates/email_digest.*.j2``)
and shipped over SMTP via aiosmtplib in a ``multipart/alternative``
envelope (HTML + plain-text).

Architecture
============

The service exposes four top-level functions plus a couple of
dataclasses; the cron loop in
``argosy/orchestrator/loops/weekly_email_digest.py`` calls
``dispatch_weekly_digest`` once per Friday-08:00-IDT tick.

  build_weekly_digest(session, user_id, *, now, window_days)
      -> WeeklyDigest

      Pure-read query layer.  Pulls:
        * top 10 most-recent monitor flags from the past N days
          (kind, severity, rationale snippet from MonitorFlag.payload)
        * top 5 open action_proposals (kind, severity, summary, deep
          link)
        * state_snapshot delta (count of snapshots in window; latest
          snapshot recap if any)
      Returns a ``WeeklyDigest`` dataclass — pure data, no I/O.

  render_digest_html(digest) -> str
  render_digest_text(digest) -> str

      Pure render — feeds the WeeklyDigest into the two Jinja
      templates.  Autoescape is enabled for BOTH ``.html.j2`` and
      ``.txt.j2`` so an LLM-generated ``rationale_md`` containing
      ``<script>...`` cannot become live HTML in the rendered body
      (codex review focus area; see XSS notes below).

  send_digest_email(to_addr, subject, html_body, text_body, *,
                    smtp_config=None) -> SendResult

      I/O layer.  Loads SMTP env vars (``ARGOSY_SMTP_HOST`` /
      ``_PORT`` / ``_USERNAME`` / ``_PASSWORD`` / ``_FROM``).  Missing
      env vars → log WARNING + return failure (don't crash the batch).
      ``TimeoutError`` / ``aiosmtplib.SMTPException`` are caught and
      converted to ``SendResult.status='failed'``.

  dispatch_weekly_digest(session, user_id, *, now, smtp_sender=None)
      -> DigestResult

      Top-level orchestrator: build → render → send → ledger row.
      Writes a ``NotificationDispatchLedger`` row with
      ``channel='email'`` so the admin UI sees the dispatch attempt
      (status='sent' or 'failed' carries the operator signal).

XSS / secrets hygiene (codex review focus)
==========================================

The digest body is built from user-supplied LLM text:
  * ``ActionProposal.summary`` — 1-2 sentence LLM-generated summary
  * ``ActionProposal.rationale_md`` — longer markdown rationale
  * ``MonitorFlag.payload`` — JSON blob with kind-specific fields

The Jinja environment used by ``_render`` enables ``autoescape`` for
ALL templates regardless of suffix.  Free-text fields rendered with
``{{ ... }}`` get HTML-escaped (``<`` → ``&lt;`` etc) so a hostile
rationale containing a ``<script>`` tag becomes inert literal text in
the rendered HTML.  We DO NOT use the ``|safe`` filter anywhere in the
bundled templates.

Secrets hygiene (spec §7.4):
  * the digest content NEVER includes account numbers, specific
    tickers (asset_class only), auth tokens, API keys, VAPID secrets,
    or the admin token.  The renderer ONLY pulls fields explicitly
    enumerated in the ``_serialize_*`` helpers below; if a future
    field appears on ActionProposal that contains secrets, the
    renderer ignores it.
  * the rationale snippet (``_snippet_for_email``) limits user-
    supplied LLM text to 240 chars to bound the surface area of any
    inadvertent secret leak from upstream.
  * the deep_link includes the user_id ONLY in a path segment
    (``/proposals/{id}?user={user_id}``) — never a token.

SMTP / TLS handling (codex review focus)
========================================

``aiosmtplib.send`` accepts ``use_tls`` (implicit TLS, port 465) and
``start_tls`` (STARTTLS upgrade after PLAIN connect, port 587).  Our
config:

  * port 465  → ``use_tls=True``, ``start_tls=False`` (implicit TLS).
  * port 587  → ``use_tls=False``, ``start_tls=True`` (STARTTLS).
  * other     → ``use_tls=False``, ``start_tls=True`` (default to the
                safer STARTTLS path; the operator can override with
                ARGOSY_SMTP_TLS_MODE=none for local-dev MTAs).

The TLS mode env override (``ARGOSY_SMTP_TLS_MODE`` ∈
``{starttls,tls,none}``) takes precedence over the port-based
inference.  Missing env vars → ``SendResult(status='skipped',
error='smtp_not_configured')`` — the digest stays visible in the
admin UI as "creds missing; set ARGOSY_SMTP_* env to activate" per
spec §7.2.

Tests
=====

The mate test module ``tests/test_email_digest.py`` covers:

  * empty digest → "no activity this week" body
  * full digest → all section headers present
  * SMTP TimeoutError → returns SendResult.failed without crashing
  * Plain-text fallback renders without HTML tags
  * No secrets in body (admin token / API key / VAPID secret strings
    are NOT present in the rendered HTML or text)
  * Loop tick happy path with stub sender
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from sqlalchemy import desc, select

from argosy.state.models import (
    ActionProposal,
    MonitorFlag,
    NotificationDispatchLedger,
    StateSnapshot,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from sqlalchemy.orm import Session


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

#: Default lookback for the weekly digest.  Friday 08:00 -> previous
#: Friday 08:00 is the natural week boundary; we use 7 days.
DEFAULT_WINDOW_DAYS: int = 7

#: Max monitor flags surfaced in the email body.  More than ~10 lines
#: in a single email becomes noise; the admin UI is the right surface
#: for the long tail.
MAX_FLAGS_IN_BODY: int = 10

#: Max open action proposals listed in the email body.  Five is the
#: "what does the user need to look at this week" budget.
MAX_PROPOSALS_IN_BODY: int = 5

#: How long a snippet of the LLM rationale we surface in the email.
#: Bounded to limit accidental secret leak surface area + keep the
#: email scannable.
RATIONALE_SNIPPET_CHARS: int = 240

#: Email subject — intentionally generic per spec §7.4 (the user's
#: provider may index subjects less strictly than bodies).
DEFAULT_SUBJECT: str = "Your weekly Argosy summary"

#: TLS mode literal — values pass through to aiosmtplib's use_tls /
#: start_tls keyword arguments.
TlsMode = Literal["starttls", "tls", "none"]


# ---------------------------------------------------------------------------
# Dataclasses (pure data; no I/O)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagEntry:
    """One row in the digest's monitor-flags section.

    Only the fields the renderer needs — extraction in ``_serialize_flag``
    is explicit so future MonitorFlag columns (account numbers,
    instrument hints, etc.) don't sneak into the email body.
    """

    kind: str
    severity: str
    surfaced_at_iso: str
    rationale_snippet: str


@dataclass(frozen=True)
class ProposalEntry:
    """One row in the digest's open-proposals section.

    Note: we surface ``kind`` (e.g. 'repatriate_currency') as a stable
    discriminator, NOT the payload's free-text instrument names.  The
    user can click through to /proposals for the structured payload.
    """

    kind: str
    severity: str
    summary: str
    deep_link: str


@dataclass(frozen=True)
class SnapshotDelta:
    """Compact summary of state-snapshot activity in the window."""

    summary: str


@dataclass(frozen=True)
class DigestSummary:
    """Counters surfaced in the "Summary" section."""

    flag_count: int
    open_proposal_count: int
    decisions_count: int
    snapshot_count: int


@dataclass(frozen=True)
class WeeklyDigest:
    """The fully-composed digest dataclass.

    ``has_any_activity`` flips False when ALL of (flags / open
    proposals / decisions / snapshots) are empty — the template uses
    it to render the single-paragraph "no activity this week" body.
    """

    user_id: str
    window_start_iso: str
    window_end_iso: str
    window_days: int
    summary: DigestSummary
    flags: list[FlagEntry]
    open_proposals: list[ProposalEntry]
    snapshot_delta: SnapshotDelta | None
    settings_link: str
    has_any_activity: bool


@dataclass(frozen=True)
class SmtpConfig:
    """Resolved SMTP credentials + TLS mode.

    Construct via ``SmtpConfig.from_env()`` which returns ``None``
    when any required field is missing (host / port / from).  The
    username + password are optional — local-dev MTAs (e.g. mailpit
    on 1025) often run without auth.
    """

    host: str
    port: int
    username: str | None
    password: str | None
    from_addr: str
    tls_mode: TlsMode

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SmtpConfig | None":
        """Load SMTP config from environment.

        Returns None when any of HOST / PORT / FROM is missing or
        the PORT can't be parsed as int.  Logs a WARNING on the
        missing-config path so operators see the reason in the log
        stream.
        """
        env = env if env is not None else os.environ.copy()
        host = env.get("ARGOSY_SMTP_HOST")
        port_str = env.get("ARGOSY_SMTP_PORT")
        from_addr = env.get("ARGOSY_SMTP_FROM")

        # Required fields.
        if not host or not port_str or not from_addr:
            _log.warning(
                "email_digest.smtp_config_missing host=%s port=%s from=%s",
                bool(host),
                bool(port_str),
                bool(from_addr),
            )
            return None
        try:
            port = int(port_str)
        except (ValueError, TypeError):
            _log.warning(
                "email_digest.smtp_port_invalid port=%r", port_str
            )
            return None

        username = env.get("ARGOSY_SMTP_USERNAME") or None
        password = env.get("ARGOSY_SMTP_PASSWORD") or None

        # TLS mode resolution.  Env override wins; otherwise infer from
        # port (465 → implicit TLS, otherwise STARTTLS).
        tls_env = (env.get("ARGOSY_SMTP_TLS_MODE") or "").lower().strip()
        tls_mode: TlsMode
        if tls_env in ("starttls", "tls", "none"):
            tls_mode = tls_env  # type: ignore[assignment]
        elif port == 465:
            tls_mode = "tls"
        else:
            tls_mode = "starttls"

        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            from_addr=from_addr,
            tls_mode=tls_mode,
        )


@dataclass(frozen=True)
class SendResult:
    """Outcome of one ``send_digest_email`` invocation.

    status:
      * 'sent'    — SMTP returned a 2xx for every recipient.
      * 'skipped' — config missing (ARGOSY_SMTP_* unset); operator
                    intent is "feature not yet activated".
      * 'failed'  — SMTP attempt raised / returned a non-2xx code.
    """

    status: Literal["sent", "skipped", "failed"]
    error: str | None = None


@dataclass(frozen=True)
class DigestResult:
    """Top-level orchestrator outcome — returned to the loop tick."""

    user_id: str
    digest: WeeklyDigest | None
    send: SendResult
    ledger_row_id: int | None = None


# ---------------------------------------------------------------------------
# Helpers — serialization
# ---------------------------------------------------------------------------


#: Regex patterns for secret-shaped strings that MUST never appear in
#: the digest body.  Codex BLOCKER (review 2026-05-30): upstream
#: agents writing to MonitorFlag.payload could theoretically embed a
#: secret-shaped string in a ``rationale_md`` field; even with
#: autoescape preventing XSS, the secret would still leak to the
#: user's inbox in plain text.  This list is intentionally generous;
#: false-positives reduce to "<redacted>" which is acceptable for an
#: email recap.
#:
#: Patterns (compiled at module load):
#:   * Anthropic / OpenAI API keys — ``sk-ant-...`` / ``sk-...``.
#:   * Argosy admin token — any value of ARGOSY_ADMIN_TOKEN env var
#:     gets redacted dynamically per-call.
#:   * VAPID private key (base64url-encoded ECDSA) — matched by the
#:     env-var redaction (no static shape because VAPID keys have no
#:     unique sentinel).
#:   * JWT-like 3-segment tokens (``aaa.bbb.ccc`` of base64url chars).
#:   * Bearer-style ``Authorization: Bearer <token>`` lines.
import re  # noqa: E402

_STATIC_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Anthropic API key prefix (``sk-ant-...``) — 95+ chars typical.
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    # OpenAI API key prefix (``sk-...``) — 40+ chars.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    # JWT-shaped tokens (three base64url segments separated by dots).
    re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}"),
    # Bearer auth header value.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{20,}"),
    # GitHub-style ghp_ / gho_ / ghu_ / ghs_ tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
)

#: Env vars whose VALUES get redacted dynamically from the email body
#: at render time.  These cover dynamic secrets (admin token, VAPID
#: private key, SMTP password) where the pattern is "whatever the env
#: currently holds, scrub it".
_DYNAMIC_SECRET_ENV_VARS: tuple[str, ...] = (
    "ARGOSY_ADMIN_TOKEN",
    "ARGOSY_VAPID_PRIVATE_KEY",
    "ARGOSY_SMTP_PASSWORD",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


def _redact_secrets(text: str) -> str:
    """Scrub secret-shaped substrings from ``text``.

    Codex review focus area — defense-in-depth: even though the
    serializer only surfaces a narrow set of ORM fields, those fields
    contain LLM-generated free-text from upstream agents.  An agent
    that accidentally quoted a secret in its rationale would otherwise
    leak it via the email.

    Two-stage scrub:
      1. Static regex set (sk-ant-..., sk-..., JWT-shape, bearer, gh_*)
      2. Dynamic env-value scrub for ARGOSY_ADMIN_TOKEN etc.

    Replacement string is ``<redacted>``; the calling renderer's
    autoescape passes it through verbatim.  False positives
    (a non-secret string that happens to match) reduce to
    ``<redacted>`` in the email — an acceptable degradation for an
    audit/recap surface.
    """
    if not text:
        return ""
    out = text
    for pat in _STATIC_SECRET_PATTERNS:
        out = pat.sub("<redacted>", out)
    # Dynamic env-var values.  Substring match is correct here — even
    # if the env contains a short value, we want it scrubbed.
    for var in _DYNAMIC_SECRET_ENV_VARS:
        val = os.environ.get(var)
        if val and len(val) >= 8 and val in out:
            out = out.replace(val, "<redacted>")
    return out


def _snippet_for_email(text: str, *, limit: int = RATIONALE_SNIPPET_CHARS) -> str:
    """Trim free-text to a bounded snippet AND scrub secret-shaped substrings.

    Why: LLM-generated rationale can include verbose multi-paragraph
    explanations; the email is a recap, not a treatise.  The bound
    also limits the surface area for inadvertent secret leak from
    upstream agents.  The secret scrub is the structural defense
    (codex BLOCKER #2 review 2026-05-30) — even if upstream embeds an
    API-key-shaped string, it never leaves the renderer.
    """
    if text is None:
        return ""
    text = _redact_secrets(str(text).strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"  # ellipsis


def _parse_flag_payload(payload_text: str | None) -> dict[str, Any]:
    """Best-effort JSON parse of ``MonitorFlag.payload``.

    Returns ``{}`` on parse failure — the renderer only needs a
    rationale snippet, so a missing / malformed payload degrades
    gracefully into "no snippet available".
    """
    if not payload_text:
        return {}
    try:
        data = json.loads(payload_text)
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _flag_rationale_snippet(flag: MonitorFlag) -> str:
    """Pull a human-readable snippet for the flag row.

    Looks at three common rationale field names in priority order:
    ``classifier_rationale`` (macro_shift flags), ``rationale_md``
    (state-observer flags), ``rationale`` (legacy).  Falls back to the
    raw payload dict reduced to "field=value, ..." form.
    """
    payload = _parse_flag_payload(flag.payload)
    for key in ("classifier_rationale", "rationale_md", "rationale"):
        val = payload.get(key)
        if val:
            return _snippet_for_email(str(val))
    # Fallback: terse one-line dict view, key=value joined by ", ".
    if payload:
        pairs = [f"{k}={v}" for k, v in list(payload.items())[:4]]
        return _snippet_for_email(", ".join(pairs))
    return ""


def _serialize_flag(flag: MonitorFlag) -> FlagEntry:
    """ORM → FlagEntry.  Explicit field-by-field copy — never spread."""
    return FlagEntry(
        kind=str(flag.kind),
        severity=str(flag.severity),
        surfaced_at_iso=_isoformat(flag.surfaced_at),
        rationale_snippet=_flag_rationale_snippet(flag),
    )


def _serialize_proposal(
    proposal: ActionProposal, *, base_url: str
) -> ProposalEntry:
    """ORM → ProposalEntry.

    Deep link convention (spec §6 §"deep_link"): ``/proposals/<id>``.
    The user_id is intentionally NOT in the URL — the receiving page
    looks it up from the session.  This avoids leaking the tenant
    identifier into any auto-forward / mail-archive surface.
    """
    return ProposalEntry(
        kind=str(proposal.kind),
        severity=str(proposal.severity),
        summary=_snippet_for_email(str(proposal.summary)),
        deep_link=f"{base_url.rstrip('/')}/proposals/{int(proposal.id)}",
    )


def _isoformat(dt: datetime | None) -> str:
    """Format an optional aware datetime as ISO 8601 (date-only)."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# build_weekly_digest — pure read
# ---------------------------------------------------------------------------


def build_weekly_digest(
    session: "Session",
    user_id: str,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    base_url: str = "http://localhost:8000",
) -> WeeklyDigest:
    """Compose the WeeklyDigest dataclass from the DB.

    Pure-read, no writes.  Returns a fully-composed dataclass even on
    "no activity" weeks (the renderer flips the "no activity" body
    branch via ``has_any_activity``).

    Args:
      session: live sync SQLAlchemy Session (the cron loop opens a
        short-lived session per tick).
      user_id: tenant.
      now: clock injection for tests; defaults to ``datetime.now(utc)``.
      window_days: lookback in days; defaults 7.
      base_url: prefix for deep-link URLs.  Production callers pull
        from ``ARGOSY_PUBLIC_URL`` or equivalent.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window_start = now - timedelta(days=window_days)

    # Flags — most recent N within window.
    flag_rows = list(
        session.execute(
            select(MonitorFlag)
            .where(
                MonitorFlag.user_id == user_id,
                MonitorFlag.surfaced_at >= window_start,
                MonitorFlag.surfaced_at <= now,
            )
            .order_by(desc(MonitorFlag.surfaced_at))
            .limit(MAX_FLAGS_IN_BODY)
        ).scalars()
    )
    flags = [_serialize_flag(f) for f in flag_rows]

    # Open proposals — current state, severity-then-recency sorted.
    proposal_rows = list(
        session.execute(
            select(ActionProposal)
            .where(
                ActionProposal.user_id == user_id,
                ActionProposal.status == "open",
            )
            .order_by(
                # Severity DESC is a synthetic ordering — SQL's text
                # ordering puts critical < info alphabetically.  We
                # post-sort in Python so this stays readable.
                desc(ActionProposal.surfaced_at)
            )
            .limit(MAX_PROPOSALS_IN_BODY)
        ).scalars()
    )
    proposals = [_serialize_proposal(p, base_url=base_url) for p in proposal_rows]
    # Stable severity-first ordering (critical, warning, info) — the
    # email reader's eyes go to the top.
    _sev_rank = {"critical": 0, "warning": 1, "info": 2}
    proposals.sort(key=lambda p: (_sev_rank.get(p.severity, 99), p.summary))

    # Decisions count — accepted+rejected+deferred status transitions
    # in the window.  The ActionProposal model tracks ``decided_at``;
    # we count rows whose decided_at falls inside the window.
    decisions_count = session.scalar(
        select(__import__("sqlalchemy").func.count(ActionProposal.id))
        .where(
            ActionProposal.user_id == user_id,
            ActionProposal.decided_at.is_not(None),
            ActionProposal.decided_at >= window_start,
            ActionProposal.decided_at <= now,
        )
    ) or 0

    # Snapshots — count + tail-end recap.
    snapshot_count = session.scalar(
        select(__import__("sqlalchemy").func.count(StateSnapshot.id))
        .where(
            StateSnapshot.user_id == user_id,
            StateSnapshot.created_at >= window_start,
            StateSnapshot.created_at <= now,
        )
    ) or 0
    snapshot_delta: SnapshotDelta | None = None
    if snapshot_count:
        snapshot_delta = SnapshotDelta(
            summary=(
                f"{snapshot_count} state snapshot"
                f"{'s' if snapshot_count != 1 else ''} captured this week. "
                "Open Argosy to see the plan-baseline diff."
            ),
        )

    # Overall summary numbers.
    open_count = session.scalar(
        select(__import__("sqlalchemy").func.count(ActionProposal.id))
        .where(
            ActionProposal.user_id == user_id,
            ActionProposal.status == "open",
        )
    ) or 0
    flag_total = session.scalar(
        select(__import__("sqlalchemy").func.count(MonitorFlag.id))
        .where(
            MonitorFlag.user_id == user_id,
            MonitorFlag.surfaced_at >= window_start,
            MonitorFlag.surfaced_at <= now,
        )
    ) or 0
    summary = DigestSummary(
        flag_count=int(flag_total),
        open_proposal_count=int(open_count),
        decisions_count=int(decisions_count),
        snapshot_count=int(snapshot_count),
    )

    has_any_activity = bool(
        flag_total or open_count or decisions_count or snapshot_count
    )

    return WeeklyDigest(
        user_id=user_id,
        window_start_iso=window_start.astimezone(timezone.utc).date().isoformat(),
        window_end_iso=now.astimezone(timezone.utc).date().isoformat(),
        window_days=window_days,
        summary=summary,
        flags=flags,
        open_proposals=proposals,
        snapshot_delta=snapshot_delta,
        settings_link=f"{base_url.rstrip('/')}/settings/notifications",
        has_any_activity=has_any_activity,
    )


# ---------------------------------------------------------------------------
# Render — Jinja
# ---------------------------------------------------------------------------


_JINJA_ENV: Any = None


def _jinja_env() -> Any:
    """Lazy-load Jinja env — autoescape ON for ALL suffixes.

    The default Jinja ``select_autoescape(['html', 'htm'])`` would
    leave the .txt.j2 fallback un-escaped; we override with a
    ``lambda _: True`` so EVERY template renders through the
    autoescape pipeline regardless of suffix.  This is the structural
    guard against an LLM-generated rationale slipping a ``<script>``
    payload past the renderer.
    """
    global _JINJA_ENV
    if _JINJA_ENV is not None:
        return _JINJA_ENV
    from jinja2 import Environment, FileSystemLoader  # noqa: PLC0415

    from argosy.templates import TEMPLATES_DIR  # noqa: PLC0415

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=lambda _name: True,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    _JINJA_ENV = env
    return env


def _reset_jinja_env_for_tests() -> None:
    """Test hook — wipe the cached env so a subsequent call rebuilds.

    Test modules that mutate ``argosy.templates.TEMPLATES_DIR`` via
    monkeypatch should call this before exercising the renderer.
    """
    global _JINJA_ENV
    _JINJA_ENV = None


def render_digest_html(digest: WeeklyDigest, *, subject: str = DEFAULT_SUBJECT) -> str:
    """Render the digest into HTML.

    Returns a fully-formed HTML document.  Body autoescapes every
    ``{{ ... }}`` expansion so user-supplied LLM text cannot inject
    live HTML / script.
    """
    env = _jinja_env()
    tmpl = env.get_template("email_digest.html.j2")
    return tmpl.render(digest=digest, subject=subject)


def render_digest_text(digest: WeeklyDigest, *, subject: str = DEFAULT_SUBJECT) -> str:
    """Render the digest into plain text (fallback for non-HTML clients)."""
    env = _jinja_env()
    tmpl = env.get_template("email_digest.txt.j2")
    return tmpl.render(digest=digest, subject=subject)


# ---------------------------------------------------------------------------
# Send — SMTP
# ---------------------------------------------------------------------------


#: Tests inject a sender to bypass real SMTP.  Signature mirrors
#: ``_aiosmtplib_sender`` below.
SmtpSender = Callable[..., Awaitable[None]]


async def _aiosmtplib_sender(
    *,
    to_addr: str,
    subject: str,
    html_body: str,
    text_body: str,
    smtp_config: SmtpConfig,
) -> None:  # pragma: no cover - real SMTP
    """Real-SMTP send via aiosmtplib (multipart/alternative).

    Body construction uses ``email.message.EmailMessage`` so the
    headers (Subject, To, From, MIME boundaries) are RFC-correct and
    we don't hand-roll the multipart boundary string.
    """
    import aiosmtplib  # noqa: PLC0415
    from email.message import EmailMessage  # noqa: PLC0415

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_config.from_addr
    msg["To"] = to_addr
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    use_tls = smtp_config.tls_mode == "tls"
    start_tls = smtp_config.tls_mode == "starttls"

    await aiosmtplib.send(
        msg,
        hostname=smtp_config.host,
        port=smtp_config.port,
        username=smtp_config.username,
        password=smtp_config.password,
        use_tls=use_tls,
        start_tls=start_tls,
    )


async def send_digest_email(
    *,
    to_addr: str,
    subject: str,
    html_body: str,
    text_body: str,
    smtp_config: SmtpConfig | None = None,
    sender: SmtpSender | None = None,
) -> SendResult:
    """Ship the rendered digest via SMTP.

    Error handling contract:
      * Missing config (``smtp_config is None`` AND env vars unset) →
        returns ``SendResult('skipped', 'smtp_not_configured')``.  This
        is the canonical "feature not yet activated" surface; the
        operator sees it in the admin UI as a missing-creds banner.
      * ``asyncio.TimeoutError`` / ``aiosmtplib.SMTPException`` /
        ``OSError`` from the SMTP layer → ``SendResult('failed',
        '<error_tag>')``.  The exception is logged at WARNING and
        swallowed — we don't want a flaky SMTP relay to crash the
        weekly cron tick.
      * Any other exception → caught, logged, returned as
        ``SendResult('failed', 'unknown_exception:<TypeName>')``.

    The function NEVER raises back to the caller.  The cron-loop
    tick relies on this so a digest failure doesn't take the loop
    offline.
    """
    if smtp_config is None:
        smtp_config = SmtpConfig.from_env()
    if smtp_config is None:
        return SendResult(status="skipped", error="smtp_not_configured")
    if not to_addr:
        _log.warning("email_digest.send_no_recipient")
        return SendResult(status="skipped", error="no_recipient")

    sender = sender or _aiosmtplib_sender
    try:
        await sender(
            to_addr=to_addr,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            smtp_config=smtp_config,
        )
    except TimeoutError as exc:
        _log.warning("email_digest.smtp_timeout error=%s", exc)
        return SendResult(status="failed", error="smtp_timeout")
    except Exception as exc:  # noqa: BLE001 — defensive; SMTP is flaky
        tag = type(exc).__name__
        _log.warning("email_digest.smtp_error tag=%s error=%s", tag, exc)
        return SendResult(status="failed", error=f"smtp_error:{tag}")
    return SendResult(status="sent")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _user_email(session: "Session", user_id: str) -> str | None:
    """Look up the user's email address.

    ``User.email`` is the NextAuth-claim column (Phase 6); pre-Phase-6
    rows have ``email IS NULL`` and the digest path returns
    'no_recipient' for them — visible in the admin UI for the
    operator to fix.
    """
    from argosy.state.models import User  # noqa: PLC0415

    row = session.get(User, user_id)
    if row is None:
        return None
    return row.email


async def dispatch_weekly_digest(
    session: "Session",
    user_id: str,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    base_url: str = "http://localhost:8000",
    smtp_sender: SmtpSender | None = None,
    to_addr: str | None = None,
) -> DigestResult:
    """Top-level orchestrator: build → render → send → ledger.

    Writes a ``NotificationDispatchLedger`` row with ``channel='email'``
    so the admin UI sees the dispatch attempt.  The status mirrors
    SendResult: 'sent' / 'failed' / 'skipped' map directly to ledger
    statuses 'sent' / 'failed' / 'skipped' (per migration 0055 CHECK
    enum).

    Args:
      session: live sync Session.  The function flushes the ledger
        row but does NOT commit — the caller (loop tick) owns the
        outer transaction.
      user_id: tenant.
      now: clock injection for tests.
      window_days: lookback.
      base_url: deep-link prefix.
      smtp_sender: stub for tests.
      to_addr: override recipient; defaults to ``User.email``.

    Returns:
      DigestResult — full provenance (digest object, send result,
      ledger row id).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    recipient = to_addr or _user_email(session, user_id)
    digest = build_weekly_digest(
        session,
        user_id,
        now=now,
        window_days=window_days,
        base_url=base_url,
    )
    html_body = render_digest_html(digest)
    text_body = render_digest_text(digest)

    send_result = await send_digest_email(
        to_addr=recipient or "",
        subject=DEFAULT_SUBJECT,
        html_body=html_body,
        text_body=text_body,
        sender=smtp_sender,
    )

    # Ledger writeback — best-effort.  Maps SendResult.status to the
    # CHECK-enum value the ledger column expects.
    #
    # Codex BLOCKER (review 2026-05-30): the ledger has UNIQUE on
    # (user_id, notification_id, channel); a same-day re-dispatch (e.g.
    # operator clicks "Run now" via /admin/jobs on a Friday the cron
    # already fired) raises IntegrityError on ``session.flush()``.
    # Two pieces of defense:
    #   1. Pre-check via SELECT — if a row already exists, return the
    #      existing id without trying to write a duplicate (cheap path).
    #   2. IntegrityError fallback — if the pre-check missed (concurrent
    #      writer beat us between SELECT and flush), catch IntegrityError
    #      AND call ``session.rollback()`` so the session isn't left in
    #      a failed state for the loop's outer commit.
    # Without the rollback the SQLAlchemy session remains broken and
    # the cron's outer ``session.commit()`` raises, crashing the tick.
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    ledger_status = send_result.status
    ledger_row_id: int | None = None
    notification_id = (
        f"weekly_digest|user:{user_id}|"
        f"{digest.window_end_iso}"
    )

    # Pre-check: same-day re-dispatch returns the existing row id
    # without IntegrityError noise.
    existing = session.execute(
        select(NotificationDispatchLedger.id).where(
            NotificationDispatchLedger.user_id == user_id,
            NotificationDispatchLedger.notification_id == notification_id,
            NotificationDispatchLedger.channel == "email",
        )
    ).scalar_one_or_none()
    if existing is not None:
        ledger_row_id = int(existing)
    else:
        row = NotificationDispatchLedger(
            user_id=user_id,
            notification_id=notification_id,
            channel="email",
            subscription_id=None,
            status=ledger_status,
            error_message=send_result.error,
        )
        session.add(row)
        try:
            session.flush()
            ledger_row_id = row.id
        except IntegrityError as exc:
            # Concurrent writer beat us — rollback so the session is
            # usable by the caller's outer commit.  Then re-read to
            # recover the winning row's id.
            session.rollback()
            _log.warning(
                "email_digest.ledger_unique_collision user=%s notification_id=%s error=%s",
                user_id,
                notification_id,
                exc,
            )
            ledger_row_id = session.execute(
                select(NotificationDispatchLedger.id).where(
                    NotificationDispatchLedger.user_id == user_id,
                    NotificationDispatchLedger.notification_id
                    == notification_id,
                    NotificationDispatchLedger.channel == "email",
                )
            ).scalar_one_or_none()
            if ledger_row_id is not None:
                ledger_row_id = int(ledger_row_id)
        except Exception as exc:  # noqa: BLE001 — audit is best-effort
            # Non-IntegrityError DB failure — still rollback so the
            # outer commit doesn't inherit a poisoned session.
            session.rollback()
            _log.warning(
                "email_digest.ledger_write_failed user=%s error=%s",
                user_id,
                exc,
            )

    _log.info(
        "email_digest.dispatch user=%s status=%s flags=%d proposals=%d has_activity=%s",
        user_id,
        send_result.status,
        len(digest.flags),
        len(digest.open_proposals),
        digest.has_any_activity,
    )

    return DigestResult(
        user_id=user_id,
        digest=digest,
        send=send_result,
        ledger_row_id=ledger_row_id,
    )


__all__ = [
    "DEFAULT_SUBJECT",
    "DEFAULT_WINDOW_DAYS",
    "DigestResult",
    "DigestSummary",
    "FlagEntry",
    "MAX_FLAGS_IN_BODY",
    "MAX_PROPOSALS_IN_BODY",
    "ProposalEntry",
    "SendResult",
    "SmtpConfig",
    "SnapshotDelta",
    "WeeklyDigest",
    "build_weekly_digest",
    "dispatch_weekly_digest",
    "render_digest_html",
    "render_digest_text",
    "send_digest_email",
]
