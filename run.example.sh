#!/bin/bash
# Copy to run.sh and fill in your keys. run.sh is gitignored.
cd "$(dirname "$0")" || exit 1

export XKIRO_API_KEY="your-key-here"
export JOBRADAR_SMTP_PASSWORD="your-gmail-app-password"

exec ./.venv/bin/python -m jobradar.main "${@:-run}" >> jobradar.log 2>&1
