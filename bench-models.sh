#!/bin/bash
# Compare candidate models on YOUR resume and a fixed set of jobs.
# Prints accuracy against known-correct answers, plus wall time per batch,
# so the model choice is made on evidence rather than vibes.
cd "$(dirname "$0")" || exit 1
eval "$(grep -E '^export CURSOR_API_KEY' run.sh)"
exec ./.venv/bin/python -m jobradar.bench "$@"
