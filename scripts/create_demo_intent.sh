#!/usr/bin/env bash
# Create a TMF921 intent from a Turtle file (demo requester helper).
# Usage: bash scripts/create_demo_intent.sh <ttl_path> [intent name]
# The broker itself has no create call; this posts straight to the intent server.
# The new intent is ACKNOWLEDGED; the server only evaluates posted observations
# for an ACTIVE intent, so the script then activates it (PATCH lifecycleStatus).
set -eu

if [ "$#" -lt 1 ] || [ -z "$1" ]; then
  echo "usage: create_demo_intent.sh <ttl_path> [intent name]" >&2
  exit 2
fi
ttl_path="$1"
name="${2:-Demo intent}"
if [ ! -f "$ttl_path" ] || [ ! -r "$ttl_path" ]; then
  echo "error: cannot read Turtle file: $ttl_path" >&2
  exit 1
fi

base="${TMF921_BASE_URL:-http://localhost:8000/tmf-api/intentManagement/v5}"

body="$(python3 -c '
import json, sys
ttl = open(sys.argv[1], encoding="utf-8").read()
print(json.dumps({"@type": "Intent", "name": sys.argv[2],
                  "expression": {"@type": "TurtleExpression", "expressionValue": ttl}}))
' "$ttl_path" "$name")"

if ! response="$(printf '%s' "$body" | curl -sf -X POST "$base/intent" -H "Content-Type: application/json" --data-binary @-)"; then
  echo "error: the intent server refused or could not be reached at $base" >&2
  exit 1
fi

if ! intent_id="$(printf '%s' "$response" | python3 -c '
import json, re, sys
i = json.load(sys.stdin).get("id")
if not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", i):
    sys.exit(1)
print(i)
')"; then
  echo "error: the intent server's reply carried no usable intent id" >&2
  exit 1
fi

if ! patched="$(curl -sf -X PATCH "$base/intent/$intent_id" -H "Content-Type: application/json" \
      --data-binary '{"lifecycleStatus": "ACTIVE"}')" \
   || ! printf '%s' "$patched" | python3 -c '
import json, sys
sys.exit(0 if json.load(sys.stdin).get("lifecycleStatus") == "ACTIVE" else 1)
'; then
  echo "error: created intent $intent_id but could not activate it" >&2
  exit 1
fi

echo "Created intent id: $intent_id"
