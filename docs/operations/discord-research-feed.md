# Passive Discord research feed

This is a read-only research input, separate from the private advisor chat bot.
It is intentionally disabled until credentials and authorized channel access
are restored and a bounded live connection is verified. Do not use a personal
Discord account token as a substitute for an authorized bot integration.

## Safety behaviour

- Maintained Discord SDK handles heartbeat acknowledgements and session RESUME.
- Connected means authenticated READY/RESUMED; disconnects show reconnecting.
- Per-credential safety counters survive process restarts. Argosy budgets are
  three login invocations, three actual login HTTP requests, and three IDENTIFY
  attempts per ten minutes; ten gateway connection attempts per ten minutes;
  and one hundred IDENTIFY attempts per rolling day. These are application
  safety ceilings, not hard-coded claims about Discord's current API quotas.
- SDK HTTP retries and repeated RESUME reconnects count at their actual network
  boundaries. OS locks reject duplicate feed listeners and overlapping history
  requests using the same credential.
- Authentication/configuration failures persist a stop and produce a failed
  job receipt without supervisor retries. Timed rate-limit cooldowns persist.
- History requests respect Retry-After/JSON retry_after and exhausted buckets;
  another worker's auth stop is checked before each physical request.
- Failed/cancelled handshakes close the constructing socket and heartbeat;
  history clients close on error and cancellation.

State is under `runtime/discord-feed/`; only credential hashes are stored, never
the token. Do not delete this state to get around a safety budget.

## Repair and activation

1. Restore a valid **bot** credential with authorized access to the source
   channel using `.venv/Scripts/python.exe -m argosy.cli.main discord-listener setup`.
   It prompts for missing source IDs and a hidden token. Optional `--guild-id`
   and `--channel-id` flags supply IDs, never the token. The token is stored
   under `discord_listener_bot_token` in the OS credential manager, separate
   from `discord_advisor_bot_token`; `~/.argosy/discord_creds.json` stores
   source IDs and a fixed `token_secret` reference. Legacy plaintext files
   remain readable; successful setup migrates them to the keychain.
   Refresh preserves saved IDs and rejects a conflicting source. Setup makes
   no network calls and does not enable the feed or clear safety counters.
   `discord-listener status` checks local configuration, not live connectivity.
   Keep tokens out of chat, git, logs and screenshots.
2. After correcting credentials/permissions, if the same credential has a
   persisted auth stop, run:

   `.venv/Scripts/python.exe -m argosy.cli.discord_ingest --clear-auth-stop`

   This exits without connecting. It does not clear timed cooldowns or budgets.
3. Verify one bounded live READY/channel-access/ingest cycle before enabling
   `discord_listener_enabled`. Do not run the CLI and the supervised listener
   concurrently. Backfill remains manual; no backlog burst is scheduled here.

## Verification

`.venv/Scripts/python.exe -m pytest tests/test_discord_feed_safety.py tests/test_discord_listener.py tests/test_discord_listener_job.py tests/test_predictions_backfill_discord.py tests/test_jobs_registry.py -q`

The wire tests use the actual installed Discord SDK against a local TCP
HTTP/WebSocket server: READY, RESUME, reconnect storms, revoked credentials,
HTTP 5xx retries, IDENTIFY rejection, handshake cancellation and construction
timeouts. A real child-process test also verifies lock exclusion and shared
budgets across process exits. They send no
requests to Discord. They do not prove live source-channel permissions or
delivery; those require the restored authorized integration.
