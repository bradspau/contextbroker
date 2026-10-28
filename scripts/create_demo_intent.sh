#!/usr/bin/env bash
# Create a TMF921 intent from a Turtle file (demo requester helper).
# Usage: bash scripts/create_demo_intent.sh <ttl_path> [intent name]
# The broker itself has no create call; this posts straight to the intent server.
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

if ! response="$(curl -sf -X POST "$base/intent" -H "Content-Type: application/json" -d "$body")"; then
  echo "error: the intent server refused or could not be reached at $base" >&2
  exit 1
fi

printf '%s' "$response" | python3 -c '
import json, sys
print("Created intent id: " + str(json.load(sys.stdin)["id"]))
'
