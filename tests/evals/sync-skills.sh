#!/usr/bin/env bash
# Rebuild .workspace/ with a fresh copy of the source skills (../../.apm/skills).
# Every promptfooconfig.yaml points working_dir at .workspace, so evals always run
# against the source skills, never the installed copy, and with no .env inside.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/../../.apm/skills"
ws="$here/.workspace"
rm -rf "$ws"
mkdir -p "$ws/.claude/skills"
cp -R "$src"/. "$ws/.claude/skills/"
find "$ws" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$ws" -name .env -type f -delete
echo "synced $(ls "$ws/.claude/skills" | wc -l | tr -d ' ') skills into $ws/.claude/skills"
