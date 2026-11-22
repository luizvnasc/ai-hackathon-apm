#!/usr/bin/env bash
# Runs in the agent container: files arrive as a tar on stdin, the result JSON leaves on stdout.
# usage: entrypoint.sh <config, relative to tests/evals> <timeout seconds> [promptfoo options]
set -euo pipefail
config=$1 limit=$2
shift 2

tar -x --no-same-owner --no-same-permissions -C /sandbox
exec </dev/null
cd /sandbox/tests/evals
bash sync-skills.sh >&2
ln -s /opt/evals/node_modules node_modules

coproc RELAY { exec python3 /opt/sandbox/relay.py; }
read -r -t 20 ready <&"${RELAY[0]}"
[ "$ready" = READY ]

status=0
timeout -k 10 "$limit" node_modules/.bin/promptfoo eval -c "$config" \
  --no-share --no-cache --no-progress-bar -o /tmp/result.json "$@" >&2 || status=$?
if [ -f /tmp/result.json ]; then cat /tmp/result.json; fi
exit "$status"
