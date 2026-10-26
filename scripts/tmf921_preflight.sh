#!/usr/bin/env bash
# Run before: docker compose --profile tmf921 up -d --build tmf921-server
# Resolves TIO_ONTOLOGY_DIR the way compose does: shell env, else the repo's .env,
# else the default; relative paths are relative to the repo root (compose project dir).
set -eu
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
dir="${TIO_ONTOLOGY_DIR:-}"
if [ -z "$dir" ] && [ -f .env ]; then
  line="$(grep -E '^[[:space:]]*TIO_ONTOLOGY_DIR=' .env | tail -n1 || true)"
  dir="${line#*=}"
  dir="${dir%$'\r'}"
  dir="${dir#\"}"; dir="${dir%\"}"; dir="${dir#\'}"; dir="${dir%\'}"
fi
dir="${dir:-../BBF_921_java/ontology}"
if [ ! -d "$dir" ]; then
  echo "ERROR: TIO_ONTOLOGY_DIR '$dir' (relative to $root) does not exist. Point it at the licensed TIO ontology directory." >&2
  exit 1
fi
if ! find "$dir" -maxdepth 1 -name '*.ttl' -print -quit | grep -q .; then
  echo "ERROR: TIO_ONTOLOGY_DIR '$dir' contains no *.ttl files; the TMF921 server needs them at start-up." >&2
  exit 1
fi
echo "OK: ontology directory '$dir' present"
