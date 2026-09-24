#!/bin/bash
# One-shot setup: venv, deps, and the weekly launchd job.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "==> creating virtualenv"
python3 -m venv .venv
./.venv/bin/pip -q install -r requirements.txt

[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "==> created config.yaml — edit it"; }

echo "==> installing schedules"
mkdir -p ~/Library/LaunchAgents
for job in com.jobradar.weekly com.jobradar.daily; do
  sed "s|__DIR__|$DIR|g" "$job.plist" > ~/Library/LaunchAgents/"$job.plist"
  launchctl unload ~/Library/LaunchAgents/"$job.plist" 2>/dev/null || true
  launchctl load  ~/Library/LaunchAgents/"$job.plist"
  echo "    $job"
done
echo "    weekly digest  Mondays 09:00"
echo "    daily alerts   every day 09:30 (watched companies only)"

cat <<'MSG'

Done. Three things left, all yours to do:
  1. Put your resume in this folder and point resume_path at it in config.yaml
  2. Edit run.sh and paste in your secrets (xkiro key + Gmail App Password)
  3. Verify email works:   ./run.sh test

MSG
