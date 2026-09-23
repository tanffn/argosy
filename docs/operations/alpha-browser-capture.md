# Meet Kevin Alpha capture

Enabled daily at **18:00 Asia/Jerusalem**, in the current user's normal Chrome
session. `configs/alpha_capture.json` selects the Argosy household and the only
Google account the chooser may select. It contains no token or password.

The existing logged-in Chrome session is authorized by the user. Password, MFA,
CAPTCHA or consent challenges are not bypassed. If needed, finish login in the
open browser and explicitly retry. Chrome may appear; terminal windows should not.
Changing focus during browser interaction aborts safely rather than operating on
another window.

## Where to look

- **Input Sources → Shared research sources → Meet Kevin Alpha (browser)**:
  capture status, latest report date, archived/queued/analyzed counts, source
  claims, review requests, and eventual outcome evaluations.
- **Jobs → alpha_capture_daily**: scheduled/manual run receipts.
- **Files**: download-only ZIP evidence. Original observation dates are retained.
- Discord news/chat retrieval can read the same household-scoped research.
  Recommendations are independent fleet conclusions, not Kevin's instructions.

## Scheduling and recovery

Argosy's registered scheduler owns the daily job. The Windows task **Argosy Alpha
Daily Capture** also runs that same registered job locally at 18:00, sign-in and unlock.
It records normal job receipts without an unauthenticated HTTP request, including
when the API is unavailable. Its windowless Python trigger checks durable state first.
There is no repeating timer
or five-minute website polling. Backend startup performs existing missed-slot
catch-up; unlock handles a previously locked desktop. A morning catch-up consumes
that calendar day's capture, so Chrome is not reopened automatically at 18:00.

The DB claim precedes browser opening and survives crashes. Failed/incomplete
attempts cannot reopen the site automatically that day. A stale attempt cannot
publish over a newer claim. Explicit retries cannot replace a still-active capture.
The authorized login hosts are exactly `app.meetkevin.com`, `sso.meet-kevin.com`,
and, after initiating Google sign-in, `accounts.google.com`. The SSO link is chosen
by its supported invocation action, not its identically named display container.
Manual capture/retry commands also produce `alpha_capture_daily` job receipts.

```powershell
# Normal once-daily path, including shared fleet queue processing:
.\.venv\Scripts\python.exe scripts/run_alpha_capture.py

# After resolving a login/browser problem; explicitly allows another attempt:
.\.venv\Scripts\python.exe scripts/run_alpha_capture.py --retry

# Recover saved evidence without reopening Chrome or rerunning paid analysis:
.\.venv\Scripts\python.exe scripts/run_alpha_capture.py --import-file PATH.html

# Install/repair the quiet event-driven Windows trigger:
.\scripts\install_alpha_capture.ps1
```

Pause the source from Input Sources to stop capture/analysis. Disable
`configs/alpha_capture.json` and restart the backend to disable the registered job
as well. The trigger independently honors disabled configuration/source state.

## Deliberate limits

- Only the posts in the saved page are covered, not complete account history.
  Daily reports are keyed by their stated date; timestamped posts by displayed
  timestamp. Ambiguous duplicate identities fail closed. Undated entries are
  content-addressed snapshots, not claimed stable remote post IDs.
- First-capture older posts are historical references, not new alerts.
- Images are decoded to verify file integrity and archived, **not interpreted**.
  Research containing images is marked partially analyzed; do not assume the fleet
  read screenshot-only trades. HTML/CSS local dependencies are checked; external
  resources are not fetched and archived JavaScript is never executed.
- One reserved daily fleet slot supplements the three general slots. Extra posts
  remain visibly queued. Cost limits still apply. Capture success does not mean
  every historical post was analyzed.
- Publication dates are author-stated, not independent proof of prior prediction.
  Reiterated forecasts receive no new scoring clock. Ambiguous horizons remain
  unscored; do not infer a track record from retrospective boasts.
- Review requests and discovery nominations are not executed trades.

## Live acceptance recorded 2026-09-22

`scripts/run_alpha_capture.py` performed actual normal-Chrome capture, automatic
Complete-format filename/save, real file-catalog import, and the shared research
fleet. Output: **10 posts, 20 images, 1 queued current report, 9 archived historical
posts; 1 analyzed; failures=[]**. Synthesis retried after one timeout and completed.
The ledger contains **119 attributed claims, 4 pending review requests, no BUY
candidates**. This does not establish profitability or completed downstream reviews.

Final importer recheck against the automatically saved page returned **unchanged=10,
queued=0**, catalog file 147 (reused on repeat import), 43 local dependency
references checked, six external resources including web fonts not fetched.
Unsupported CSS-rendered visual evidence is rejected rather than silently omitted.
The deployed source API reports captured/no errors, and the
registered job's repeat run reports already_captured without another browser opening.
