# Private Discord advisor

This is a separate advisory interface. It does not enable the old passive
`discord_listener`, approve trades, record fills, edit the plan, or operate a
broker. Explicit ticker reviews use the canonical fleet in persisted
`analysis_only` mode. The PC must be awake and online.

## Private server setup

1. Create a private server and a text channel, for example `#argosy`. Remove
   `View Channel` from `@everyone`; allow only the owner and the bot. Anyone who
   can read the channel can see the financial information posted there.
2. Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications).
   Enable **Message Content Intent** under Bot. Do not grant Administrator.
3. Install it into that server with View Channel, Send Messages, Read Message
   History and Send Messages in Threads. Keep the token local and private.
4. Enable Developer Mode in Discord. Copy the server ID, channel ID and your
   own user ID. They are not the display names.
5. From the repository directory, run:

   ```powershell
   .venv/Scripts/python.exe -m argosy.cli.main discord-advisor setup --guild-id SERVER_ID --channel-id CHANNEL_ID --discord-user-id YOUR_USER_ID --user-id ariel
   ```

   The token prompt is hidden. The token is saved in the OS keychain under
   `argosy / discord_advisor_bot_token`, never in the repository or Discord chat.
   Non-secret settings are in `configs/discord_advisor.json` under Argosy's
   configured home. Re-running setup preserves preferences and refuses to
   silently replace a different identity binding.
6. Restart the backend using the existing silent launcher. The homepage reports
   actual connection state. Missing setup is neutral; a rejected token or a
   stopped configured worker is actionable. Do not expose the local API publicly.

Discord receives the content Argosy sends. The bot ignores DMs, other channels,
unbound people, other bots and webhooks. Identity is checked before retrieval or
LLM calls. Adding another person requires an explicit local binding.

References: [Gateway and intents](https://docs.discord.com/developers/events/gateway),
[message creation and bounded nonce deduplication](https://github.com/discord/discord-api-docs/blob/main/developers/resources/message.mdx).

## Use

Ask naturally in the configured channel:

- “What are my current actions?” or “Why did you recommend X?” reads saved records.
- “How many research subscriptions do we follow?” reads registered sources; it
  does not sync unrelated personal accounts or launch investment research.
- “What did channel X say recently?” summarizes material actually ingested.
- “Run the fleet on X” starts a bounded analysis-only review. Progress names only
  agents with durable receipts. Reused reports and incomplete runs are identified.
- “System health?” and “How did our calls do?” read actual receipts and recorded
  outcome statistics, including missing coverage.

No ordinary question authorizes plan changes or orders. Defaults are three
instruments per request and one active fleet per household. Existing LLM budget
and authentication controls still apply. User-requested replies can arrive during
quiet hours. A grounded daily briefing (at most three useful points, 100 words)
summarizes saved news, discovery research, and the current trade plan in the
context of holdings. It is not an Inbox-count digest or a fresh fleet verdict.
The existing answer agent writes it and the independent coverage agent reviews
it. Old research is dated; missing/stale coverage is distinguished from no
worthwhile update. Drafting runs in the background so urgent alerts do not wait.
The brief arrives at 09:00 in
the configured timezone (`daily_overview_time`), or after wake-up that day. No
backlog of missed daily summaries is sent. Delivery is deduplicated durably by
binding and local date, including restarts and intraday recommendation changes.
Unsent drafts survive retries; changed canonical actions invalidate them before
delivery. Preview the real read/review path without sending with
`.venv/Scripts/python.exe scripts/preview_discord_daily.py`. Its explicit
`--replace-today` operator option updates only today's existing bot-authored
brief, preserving the prior text in `tmp/discord-daily-preview.json`.
Only upstream critical Inbox flags and enabled jobs with canonical RED health
push immediately, including quiet hours; unchanged retries do not repeat alerts.
Resolved alerts close; a new recurrence can alert again. Full evidence remains
stored for follow-up questions, without source dumps in unsolicited messages.
Chat defaults to 1–3 short sentences; overview answers use at most three short
bullets unless depth is requested. Internal record IDs remain in persisted
provenance, not visible source dumps. Greetings get a greeting, not a menu.

Authorized incoming messages receive an immediate neutral ⏳ reaction when Discord
allows reactions. The existing intent router chooses a contextual emoji (without
another model call or keyword/ticker rules); it replaces the neutral reaction.
One durable progress message changes as routing, record
collection and answer generation actually occur; the same message becomes the
answer, then ✅ replaces the contextual reaction. Missing reaction permissions do not block replies.
Follow-up routing sees prior conversation as untrusted referent context, never
permission to dispatch research. Factual identity uncertainty is checked against
the current public SEC issuer directory before asking the user to resolve it.
This fixed-URL lookup sends no household data, uses the existing SEC contact
configuration, has an eight-second network timeout and six-hour dated memory
cache. Missing/non-US/private identities remain explicitly unverified; no mapping
is invented. True user-intent ambiguity still requires clarification;
instrument-specific reads enforce the ticker selector instead of returning an
unrelated default page. Market-headline reads combine public news signals and
household research, explicitly distinguishing ingest time from publication time
and cached coverage from live market coverage. Private unowned Discord signals
are excluded from the shared public-news read.

## Private scheduled-task board and branding

An optional `status_channel_id` on each binding selects a second private channel.
It is an outbound board, not another chat entry point. The worker uses the actual
registered jobs, cadence state and successful run receipts to show schedules,
last success, current runs, next runs and failures. It edits the same persisted
messages every 60 seconds without mentions. A last-refreshed timestamp makes
sleep/offline staleness explicit; failed reads retain the prior board with a
failure notice instead of claiming everything is healthy.

`scripts/setup_discord_channels.py` creates `argosy-chat` and `argosy-status` with
explicit private permissions and saves their binding, preserving old channels
and messages. This operator command needs Manage Channels. The cosmetic command
`scripts/brand_discord_advisor.py --apply` reuses the app's existing logo for the
bot/application/server and updates description/topic; it additionally needs
Manage Server. Preview by omitting `--apply`. These are local maintenance tools,
not administrative capabilities exposed to the conversational model. Never
grant Administrator for this setup. Server banners depend on Discord's enabled
server features; these commands do not purchase boosts.

Runtime access/auth errors stop safely. An explicit supported job reconnect
reloads saved configuration and credentials and revalidates the corrected setup.
Every outgoing message/edit also checks channel privacy. Initial recovery is
bounded to messages sent after the durable authorized binding was created;
pre-authorization channel history is never replayed. Subsequent recovery uses
the last received-message cursor.
When an active scheduled run prevents a safe backend reload,
`scripts/deploy_discord_when_idle.py` waits up to three hours without cancelling
work, runs the verified silent reloader, checks connection and board receipts,
then sends a ready message. Its result is in `tmp/discord-deployment.json`.

## Verification and limitations

```powershell
.venv/Scripts/python.exe scripts/check_discord_advisor.py --user ariel
.venv/Scripts/python.exe -m argosy.cli.main discord-advisor status
```

The first command exercises real database retrieval without launching an LLM.
The second checks configuration, not connectivity. The live backend exposes
`GET /health/discord-advisor`; it returns no token or private identity IDs.

Database and fake-gateway tests do **not** prove live Discord delivery. Live
acceptance requires a real authorized message, saved turn/request/run IDs,
progress edits and a final message ID. Reconnect/catch-up and outbox receipts
reduce duplicate delivery; Discord nonce deduplication covers only a short
window and is not unlimited exactly-once delivery.
