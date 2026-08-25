#!/usr/bin/env bash
# Replace the over-matching UserPromptSubmit "handover" grep hook with a /handover
# slash command. Run from the repo root:  bash scratchpad/install_handover_cmd.sh
set -euo pipefail

cd "$(dirname "$0")/.."
SETTINGS=".claude/settings.local.json"
PY="D:/Projects/financial-advisor/.venv/Scripts/python.exe"

if [ ! -f "$SETTINGS" ]; then
  echo "ERROR: $SETTINGS not found (cwd: $PWD)" >&2
  exit 1
fi

# --- 1. back up (skip if the earlier attempt already made one) -----------------
if [ ! -f "$SETTINGS.bak" ]; then
  cp "$SETTINGS" "$SETTINGS.bak"
  echo "backed up -> $SETTINGS.bak"
else
  echo "backup already exists, keeping it -> $SETTINGS.bak"
fi

# --- 2. drop the UserPromptSubmit hook, keep everything else -------------------
"$PY" - "$SETTINGS" <<'PY'
import collections
import io
import json
import sys

path = sys.argv[1]
with io.open(path, encoding="utf-8") as fh:
    data = json.load(fh, object_pairs_hook=collections.OrderedDict)

hooks = data.get("hooks", {})
if hooks.pop("UserPromptSubmit", None) is None:
    print("UserPromptSubmit hook already absent - nothing to remove")
else:
    print("removed the UserPromptSubmit hook")

assert "Stop" in hooks, "the Stop hook must survive this edit"

with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(data, indent=2) + "\n")

# re-parse: a malformed settings file silently disables ALL settings in it
with io.open(path, encoding="utf-8") as fh:
    json.load(fh)

print("hooks now:", list(hooks))
print("permissions.allow entries preserved:", len(data.get("permissions", {}).get("allow", [])))
PY

# --- 3. install the slash command ---------------------------------------------
mkdir -p .claude/commands
cp scratchpad/handover_cmd.md .claude/commands/handover.md
echo "installed -> .claude/commands/handover.md ($(wc -l < .claude/commands/handover.md) lines)"

echo
echo "--- done. /handover appears after a config reload (open /hooks, or restart). ---"
echo "--- rollback: cp $SETTINGS.bak $SETTINGS ---"
