#!/usr/bin/env bash
# Argosy live-integration smoke test.
# Hits every /api/* route through the Next.js proxy on :1337 and prints
# the HTTP status. Read-only / safe — POSTs are sent with empty bodies
# so 422 means "route exists, validation rejected" (still a pass).

set -u
BASE="http://localhost:1337"

probe() {
  local method="$1"; shift
  local path="$1"; shift
  local body="${1:-}"
  local code
  if [[ "$method" == "GET" ]]; then
    code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE$path")
  else
    code=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" \
      -H "Content-Type: application/json" \
      -d "$body" \
      "$BASE$path")
  fi
  printf "%-3s %-45s %s\n" "$method" "$path" "$code"
}

echo "=== Phase 0 ==="
probe GET /api/health

echo "=== Phase 2 (read-only) ==="
probe GET "/api/portfolio/snapshot?user_id=ariel"
probe GET "/api/plan/current?user_id=ariel"
probe GET "/api/daily-brief/latest?user_id=ariel"
probe GET "/api/agent-activity?user_id=ariel"

echo "=== Phase 3 ==="
probe GET "/api/proposals?user_id=ariel"
probe POST /api/decisions/run '{"user_id":"ariel","ticker":"AAPL","tier":"auto"}'

echo "=== Phase 4 ==="
probe GET "/api/lots?user_id=ariel"
probe GET "/api/fills?user_id=ariel"
probe GET "/api/audit?user_id=ariel"

echo "=== Phase 5 ==="
probe GET "/api/argonaut/status?user_id=ariel"
probe GET "/api/argonaut/snapshots?user_id=ariel"
probe GET "/api/argonaut/trades?user_id=ariel"
probe POST /api/security/totp/setup '{"user_id":"ariel"}'

echo "=== Phase 6 ==="
probe GET "/api/branding?user_id=ariel"
probe POST /api/onboarding/redeem '{"token":"x"}'

echo "=== Phase 7 ==="
probe GET "/api/domain-kb/tree"
probe GET "/api/domain-kb/file?path=tax/israel/brackets_2026.md"
probe GET "/api/domain-kb/review-queue"
probe GET "/api/intake/status?user_id=ariel"
probe POST /api/intake/turn '{"user_id":"ariel","answer":""}'
probe GET "/api/settings?user_id=ariel"

echo "=== Done ==="
