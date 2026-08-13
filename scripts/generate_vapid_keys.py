"""Generate VAPID credentials and write them to ~/.argosy/vapid_creds.json.

Usage
-----
    .venv/Scripts/python.exe scripts/generate_vapid_keys.py

What this does
--------------
Generates a fresh P-256 ECDSA key pair suitable for RFC 8292 VAPID
signing, then writes the keys to ``~/.argosy/vapid_creds.json`` in the
format expected by ``argosy.services.web_push._load_vapid_creds``:

    {
      "vapid_public_key":  "<base64url-encoded uncompressed P-256 point>",
      "vapid_private_key": "<PEM-encoded EC private key>",
      "subject_uri":       "mailto:arieljacob@gmail.com"
    }

After running this script:
  1. Restart the API server (``uvicorn argosy.api.main:create_app --factory ...``).
  2. Open http://localhost:1337/settings/notifications in Chrome.
  3. Click "Enable notifications" — the card will call pushManager.subscribe()
     using the key just loaded from the server, then POST the resulting
     PushSubscription to /api/notifications/subscribe.
  4. Verify with:
     sqlite3 db/argosy.db "SELECT count(*) FROM notification_subscriptions;"
     (should read 1 after step 3).

The file lives OUTSIDE the repo (not committed).  Re-running this script
regenerates the key pair; existing browser subscriptions become invalid
and need to be re-subscribed via the UI.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
except ImportError:
    sys.exit(
        "cryptography package not found.  "
        "Run: uv pip install cryptography"
    )

SUBJECT_URI = "mailto:arieljacob@gmail.com"
OUTPUT_PATH = Path.home() / ".argosy" / "vapid_creds.json"


def generate() -> dict[str, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    public_key = private_key.public_key()
    # Uncompressed point (65 bytes starting with 0x04) → base64url, no padding.
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64url = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("utf-8")

    return {
        "vapid_public_key": public_b64url,
        "vapid_private_key": private_pem,
        "subject_uri": SUBJECT_URI,
    }


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    creds = generate()
    OUTPUT_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    print(f"VAPID credentials written to {OUTPUT_PATH}")
    print(f"  vapid_public_key : {creds['vapid_public_key'][:30]}...")
    print(f"  subject_uri      : {creds['subject_uri']}")
    print()
    print("Next steps:")
    print("  1. Restart the API server.")
    print("  2. Open http://localhost:1337/settings/notifications in Chrome.")
    print("  3. Click 'Enable notifications' — subscribe this browser.")
    print("  4. Verify: sqlite3 db/argosy.db \"SELECT count(*) FROM notification_subscriptions;\"")


if __name__ == "__main__":
    main()
