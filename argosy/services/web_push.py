"""Web push (VAPID) sender — Spec E commit #3.

Sends VAPID-signed POSTs to browser-vendor push services per RFC 8030
+ RFC 8292 (VAPID).  Used exclusively by
``argosy.services.notification_dispatcher.dispatch_notification`` when
fanning out to ``channel='web_push'`` subscriptions.

What this module does
=====================

1. **Loads VAPID credentials** from ``~/.argosy/vapid_creds.json`` (a
   per-machine secret file).  The file shape:

       {
         "vapid_public_key":  "<base64url-encoded uncompressed P-256 public key>",
         "vapid_private_key": "<PEM-encoded EC private key (P-256)>",
         "subject_uri":       "mailto:arieljacob@gmail.com"
       }

   If the file is missing OR malformed, ``send_web_push`` returns a
   ``WebPushResult(status='failed', error='vapid_not_configured')``
   instead of crashing.  This is by design — operators may stand up
   the rest of the dispatcher (in-app channel) before generating VAPID
   keys.  The caller (notification_dispatcher) treats every web_push
   failure as a degraded-but-non-fatal outcome.

2. **Validates endpoint shape** per spec §3.4 + codex BLOCKER #4 from
   the spec text.  The check is intentionally LOOSE — we want to
   reject obviously-broken URLs (non-https, missing host) WITHOUT
   pinning to a hard host allowlist that would silently break legit
   subscriptions when browser vendors rotate push endpoints.  Two
   structural checks:

     * scheme MUST be ``https`` (web push runs over TLS only);
     * hostname MUST be non-empty after URL parsing.

   On reject the returned result carries
   ``status='failed', error='invalid_endpoint_<reason>'`` and the
   caller never attempts the HTTP POST.  Telemetry-tagged with the
   parsed host so ops dashboards can spot unknown vendors without
   gating delivery.

3. **Signs a VAPID JWT** (ES256 over the ECDSA P-256 private key).
   The JWT claims per RFC 8292 §2:

     * ``aud``: scheme + host of the push endpoint (origin only);
     * ``exp``: now + 12h (VAPID spec caps at 24h; 12h is a safer
       middle).
     * ``sub``: the operator's ``mailto:`` URI.

   Two request headers go on the POST:

     * ``Authorization: vapid t=<jwt>, k=<vapid_public_key_b64url>``
       (the "vapid" auth scheme; servers parse both forms but the
       single-header ``Authorization`` variant is the modern form per
       RFC 8292 §3).
     * ``TTL: <seconds>`` (RFC 8030 §5.2 — required header for push
       requests; we default to 24h so a momentarily-offline browser
       still picks the message up).

4. **POSTs the payload to ``subscription.endpoint``.** v1 sends the
   payload **unencrypted** — the body is the JSON-stringified
   notification dict.  RFC 8291 ("Message Encryption for Web Push") is
   REQUIRED for browser-side display, so this v1 path will work for
   *push-endpoint health checks* and *server-acknowledged delivery*
   but the browser SW's ``push`` event handler will see an empty
   ``data`` field (per the W3C Push API spec: a push without a
   readable body still fires the event; the SW can ``self.showNotification``
   with a static message).  Implementing AES128GCM content-encoding
   (RFC 8291) is a follow-on commit; the spec explicitly carves this
   out as acceptable for v1 ("v1 sends UNENCRYPTED title+body via the
   VAPID JWT only … User's primary channel for v1 will be in-app +
   email anyway").  The follow-on TODO is captured below.

5. **Maps HTTP status to lifecycle:**

     * 2xx                 → ``status='sent'``
     * 404 / 410           → ``status='gone'`` (browser uninstalled SW
                              or user revoked permission; the caller
                              flips ``NotificationSubscription.status``
                              to ``'gone'`` and skips this row on
                              future dispatches).
     * any other non-2xx   → ``status='failed'`` with
                              ``http_status`` + ``error`` populated.

Concurrency
===========

Async-only.  The dispatcher fans out to multiple subscriptions; each
``send_web_push`` is a single ``httpx.AsyncClient.post`` so the caller
can ``asyncio.gather`` if it wants parallel send (today the dispatcher
serializes per-subscription for simpler error attribution).

TODO (v2)
=========

* **RFC 8291 AES128GCM body encryption** (so the SW's ``event.data``
  is non-empty and the SW can render the title/body from the message
  itself instead of a static template).  Adds ~80 lines of crypto;
  deferred per spec §3.4.
* **Topic header** (RFC 8030 §5.4) — for repeat-notification coalescing
  on browser-vendor side.  Currently the dispatcher's own dedup_ledger
  guarantees one-server-side-send-per-day; the Topic header would let
  vendors collapse client-side too if our dedup ever missed.
* **AWS SES-style backoff/retry** — current code sends once and
  attributes any 5xx as ``failed``.  Spec defers retry/backoff to a
  housekeeping loop (out of v1 scope).
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization

if TYPE_CHECKING:  # pragma: no cover — typing only
    from argosy.state.models import NotificationSubscription


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default path for the VAPID credentials file.  Operators stash the
#: file outside the repo (this directory is per-user, on the local
#: machine, not synced).
DEFAULT_VAPID_CREDS_PATH: Path = Path.home() / ".argosy" / "vapid_creds.json"

#: VAPID subject URI bound to the user's personal email per the binding
#: in CLAUDE.md / user-email preference.  This is the fallback used when
#: the creds file doesn't carry an explicit ``subject_uri``.
DEFAULT_VAPID_SUBJECT: str = "mailto:arieljacob@gmail.com"

#: JWT exp window (RFC 8292 §2 caps at 24h; 12h is the standard
#: pywebpush-style default).
_JWT_EXP_SECONDS: int = 12 * 60 * 60

#: TTL header default — 24h (RFC 8030 §5.2).  A momentarily-offline
#: browser will still pick the message up when it reconnects.
_PUSH_TTL_SECONDS: int = 24 * 60 * 60

#: HTTP timeout for the push POST.  Browser push services are usually
#: <1s; 10s is a safe upper bound that still aborts on a stuck
#: connection.
_HTTP_TIMEOUT_SECONDS: float = 10.0


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebPushResult:
    """Outcome of one ``send_web_push`` call.

    Fields:
      status: ``'sent'`` (2xx from the push endpoint), ``'gone'``
        (404 / 410 — subscription is dead; caller should flip
        ``NotificationSubscription.status`` to ``'gone'``), ``'failed'``
        (any other non-2xx OR a pre-flight rejection like
        ``vapid_not_configured`` / ``invalid_endpoint_*``).
      error: human-readable error tag.  NULL on ``sent``.
        Conventional tags: ``vapid_not_configured``,
        ``invalid_endpoint_scheme``, ``invalid_endpoint_host``,
        ``invalid_endpoint_url``, ``http_<status>``,
        ``transport_error``.
      http_status: the upstream HTTP status, when one was returned.
        NULL for pre-flight failures (no request sent).
      telemetry_endpoint_host: parsed hostname of the push endpoint —
        emitted on every call regardless of outcome so the caller's
        dispatch ledger can tag downstream telemetry by vendor
        (fcm.googleapis.com / web.push.apple.com / etc.) without
        baking a hard allowlist into the gate.
    """

    status: Literal["sent", "gone", "failed"]
    error: str | None = None
    http_status: int | None = None
    telemetry_endpoint_host: str | None = None


@dataclass(frozen=True)
class _VapidCreds:
    """Loaded VAPID credentials (private key + public key + subject)."""

    private_key_pem: str
    public_key_b64url: str
    subject_uri: str


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_vapid_creds(
    creds_path: Path | None = None,
) -> _VapidCreds | None:
    """Load the VAPID creds file; return None if missing/malformed.

    The caller treats ``None`` as "VAPID is not configured on this
    machine" and surfaces a ``WebPushResult(status='failed',
    error='vapid_not_configured')`` without raising.
    """
    path = creds_path or DEFAULT_VAPID_CREDS_PATH
    if not path.exists():
        _log.warning(
            "vapid_creds_missing path=%s — web push disabled until "
            "creds file is created.  Generate via py-vapid (see "
            "argosy/services/web_push.py docstring).",
            path,
        )
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning(
            "vapid_creds_malformed path=%s exc=%s — web push disabled.",
            path,
            exc,
        )
        return None
    try:
        private_key_pem = str(raw["vapid_private_key"])
        public_key_b64url = str(raw["vapid_public_key"])
    except (KeyError, TypeError) as exc:
        _log.warning(
            "vapid_creds_missing_keys path=%s exc=%s — expected "
            "'vapid_public_key' + 'vapid_private_key' fields.",
            path,
            exc,
        )
        return None
    subject_uri = str(raw.get("subject_uri") or DEFAULT_VAPID_SUBJECT)
    # Smoke-test the private key parses; if not, treat as misconfigured
    # rather than waiting for the first JWT sign attempt to crash.
    try:
        serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
    except Exception as exc:  # noqa: BLE001 — any parse failure means bad creds
        _log.warning(
            "vapid_private_key_unloadable path=%s exc=%s — "
            "web push disabled.",
            path,
            exc,
        )
        return None
    return _VapidCreds(
        private_key_pem=private_key_pem,
        public_key_b64url=public_key_b64url,
        subject_uri=subject_uri,
    )


def _validate_endpoint_shape(
    endpoint: str,
) -> tuple[bool, str | None, str | None]:
    """Spec §3.4 + codex BLOCKER #4: shape-only validation.

    Returns ``(is_valid, error_tag, hostname)``.

    Loose by design — we DO NOT pin a host allowlist (vendors rotate
    hostnames; enterprise proxies surface unknown hosts).  Only
    structural defects fail:

      * scheme != 'https'              → ``invalid_endpoint_scheme``
      * empty/missing hostname         → ``invalid_endpoint_host``
      * any urlsplit() exception       → ``invalid_endpoint_url``

    The ``hostname`` second return value is the telemetry tag the
    caller plumbs into the dispatch ledger regardless of the validity
    verdict.
    """
    try:
        parts = urlsplit(endpoint)
    except (ValueError, TypeError):
        return False, "invalid_endpoint_url", None
    host = parts.hostname  # lowercased, IDNA-decoded by urlsplit
    if parts.scheme.lower() != "https":
        return False, "invalid_endpoint_scheme", host
    if not host:
        return False, "invalid_endpoint_host", host
    return True, None, host


def _sign_vapid_jwt(creds: _VapidCreds, endpoint: str) -> str:
    """Return the VAPID JWT for a single ``endpoint``.

    ``aud`` is the scheme + host of the endpoint (the *origin*), per
    RFC 8292 §2.  The JWT is signed with ES256 over the loaded P-256
    private key.
    """
    parts = urlsplit(endpoint)
    aud = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        aud = f"{aud}:{parts.port}"
    claims = {
        "aud": aud,
        "exp": int(time.time()) + _JWT_EXP_SECONDS,
        "sub": creds.subject_uri,
    }
    token = jwt.encode(
        claims,
        creds.private_key_pem,
        algorithm="ES256",
    )
    # PyJWT returns ``str`` since 2.0; defensive cast for any legacy path.
    return token if isinstance(token, str) else token.decode("ascii")


def _build_headers(jwt_token: str, public_key_b64url: str) -> dict[str, str]:
    """Compose the VAPID + RFC 8030 headers for the push POST."""
    return {
        "Authorization": f"vapid t={jwt_token}, k={public_key_b64url}",
        "TTL": str(_PUSH_TTL_SECONDS),
        "Content-Type": "application/json",
        # ``aes128gcm`` is the modern content-encoding identifier; we
        # do NOT actually encrypt in v1, so a missing
        # Content-Encoding is correct for the unencrypted path.  When
        # the v2 follow-on lands, this header switches to
        # ``aes128gcm`` per RFC 8291.
    }


def _encode_payload(payload: dict[str, Any]) -> bytes:
    """JSON-serialise the payload (unencrypted v1 — see module docstring)."""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# Re-exported for tests that want to assert the helper's behaviour
# directly without hitting an HTTP transport.
__test_helpers__ = (
    "_validate_endpoint_shape",
    "_load_vapid_creds",
    "_sign_vapid_jwt",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def send_web_push(
    subscription: "NotificationSubscription",
    payload: dict[str, Any],
    *,
    creds_path: Path | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> WebPushResult:
    """Send one VAPID-signed push to a single ``NotificationSubscription``.

    Args:
      subscription: the active push subscription row.  Must have
        ``channel == 'web_push'`` (caller's responsibility).
      payload: the message body (JSON-serialisable).  Today this is
        sent UNENCRYPTED per the module docstring's v1 carve-out.
      creds_path: override the VAPID creds file location (tests).
      http_client: inject an httpx ``AsyncClient`` (tests).  When
        ``None`` a one-shot client is created with the default
        timeout.

    Returns:
      WebPushResult — see dataclass docstring for the status / error
      tag matrix.

    Side effects:
      Logs WARNING on pre-flight failures (vapid_not_configured,
      invalid endpoint shape) and on transport errors.  The DB write
      of the dispatch_ledger row is the caller's responsibility; this
      function is pure I/O + sign.
    """
    endpoint = subscription.endpoint
    is_valid, err_tag, host = _validate_endpoint_shape(endpoint)
    if not is_valid:
        _log.warning(
            "web_push_invalid_endpoint sub_id=%s endpoint=%r err=%s",
            getattr(subscription, "id", None),
            endpoint,
            err_tag,
        )
        return WebPushResult(
            status="failed",
            error=err_tag,
            telemetry_endpoint_host=host,
        )

    creds = _load_vapid_creds(creds_path)
    if creds is None:
        return WebPushResult(
            status="failed",
            error="vapid_not_configured",
            telemetry_endpoint_host=host,
        )

    try:
        token = _sign_vapid_jwt(creds, endpoint)
    except Exception as exc:  # noqa: BLE001 — any sign failure is bad creds
        _log.exception(
            "web_push_jwt_sign_failed sub_id=%s endpoint=%r",
            getattr(subscription, "id", None),
            endpoint,
        )
        return WebPushResult(
            status="failed",
            error=f"jwt_sign_error:{type(exc).__name__}",
            telemetry_endpoint_host=host,
        )

    headers = _build_headers(token, creds.public_key_b64url)
    body = _encode_payload(payload)

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        try:
            response = await client.post(endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            _log.warning(
                "web_push_transport_error sub_id=%s endpoint=%r exc=%s",
                getattr(subscription, "id", None),
                endpoint,
                exc,
            )
            return WebPushResult(
                status="failed",
                error="transport_error",
                telemetry_endpoint_host=host,
            )
    finally:
        if owns_client:
            await client.aclose()

    http_status = int(response.status_code)
    if 200 <= http_status < 300:
        return WebPushResult(
            status="sent",
            http_status=http_status,
            telemetry_endpoint_host=host,
        )
    if http_status in (404, 410):
        # Browser uninstalled SW / user revoked permission.  Caller
        # flips subscription.status to 'gone'.
        return WebPushResult(
            status="gone",
            http_status=http_status,
            error=f"http_{http_status}",
            telemetry_endpoint_host=host,
        )
    # All other non-2xx — log + return failed.
    _log.warning(
        "web_push_non2xx sub_id=%s endpoint=%r http_status=%s "
        "body=%r",
        getattr(subscription, "id", None),
        endpoint,
        http_status,
        response.text[:200] if hasattr(response, "text") else "<no body>",
    )
    return WebPushResult(
        status="failed",
        error=f"http_{http_status}",
        http_status=http_status,
        telemetry_endpoint_host=host,
    )


# Unused-import stubs kept for clarity of intent; real code-paths use
# them through the module-private helpers above.
_UNUSED = (base64,)


__all__ = [
    "DEFAULT_VAPID_CREDS_PATH",
    "DEFAULT_VAPID_SUBJECT",
    "WebPushResult",
    "send_web_push",
]
