---
description: Update docs/handovers/HANDOVER.md + the CLAUDE.md pointer, then emit next-session start text
allowed-tools: Bash(git *), Read, Edit, Write, Grep, Glob
---

Run the Argosy handover routine. Do the work, then report — do not announce and stop.

## 1. Establish the real state

    git log --oneline -14
    git status --short

Read the top `START HERE` block of `docs/handovers/HANDOVER.md`. Trust `git log` over dated prose.

## 2. Update `docs/handovers/HANDOVER.md` in place

- **This is the ONLY handover file. Do NOT create a dated sibling** — the 33 that used to
  exist were consolidated on 2026-08-12 and deleted.
- Refresh the `START HERE` block: correct HEAD sha, current position/book figures, what
  landed this session, what is open, what is blocked on Ariel.
- Move anything the session superseded out of the top block; older blocks below stay.
- If the working tree is dirty, **read the diff and say what it is.** Uncommitted work has
  twice been mislabelled "not mine, intent unknown" and nearly discarded — once it was a
  standing user ruling.

## 3. Update the `CLAUDE.md` fresh-start pointer

The paragraph beginning **"Fresh session: start at `docs/handovers/HANDOVER.md`"** — update
the block title and the HEAD sha to match what you just wrote. Leave the rest of CLAUDE.md
alone.

## 4. Commit — handover + CLAUDE.md ONLY

Do not sweep unrelated working-tree changes into the handover commit. If they belong in git,
say so and let Ariel decide; commit them separately if he agrees.

Shell note: this is Git Bash. Use a heredoc (`git commit -F - <<'EOF'`) for the message —
PowerShell here-strings produce a mangled commit here.

## 5. Emit the copy-paste start text

A fenced block the next session can be started with. Cover, in this order:

1. **What to read first** — the handover block title, the HEAD sha, `git log --oneline -14`.
2. **Current state** — what is settled and must NOT be re-derived, with the file and the
   tested-code path that hold it; the position/book snapshot.
3. **First task** — one concrete next action.
4. **Working discipline** — the standing corrections:
   - Don't ask Ariel derivation questions ("isn't that Argosy's job?"). Escalate only
     structurally different PATHS.
   - Don't announce work and then stop.
   - Green service tests prove nothing about the API — check the DTO.
   - A claim of success cites a command that ran the REAL path, and its output.
   - Verify before asserting; execute the mechanism rather than trusting the comment.
5. **Open items** — split into *not blocked on Ariel* vs *blocked on Ariel*.
