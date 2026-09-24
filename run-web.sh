#!/bin/bash
# Starts the web UI. Secrets come from run.sh so they live in exactly one place.
cd "$(dirname "$0")" || exit 1
eval "$(grep -E '^export (XKIRO|CURSOR|TYPESAFE|JOBRADAR)_' run.sh)"
exec ./.venv/bin/uvicorn jobradar.web.server:app --host 127.0.0.1 --port 8765 "$@"
