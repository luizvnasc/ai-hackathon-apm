#!/usr/bin/env bash
# Setup wizard for the hackathon-metrics APM package.
#
# Registers the MCP servers this package needs (currently: GitLab) in the
# consumer project's apm.yml through `apm install --mcp`, so APM writes the
# right config for every harness it detects (.mcp.json for Claude Code,
# .vscode/mcp.json, .cursor/mcp.json, ...).
#
# Interactive by default; every question can be answered with a flag so the
# /setup-mcp command (or CI) can run it non-interactively.
#
# Usage: setup-mcp.sh [options]
#   --gitlab-url URL     GitLab base URL (default: $GITLAB_URL or http://localhost:8929)
#   --auth oauth|token   oauth (default; works on GitLab CE) or a token header read
#                        from an env var at runtime (never written to disk)
#   --token-var NAME     env var holding the token when --auth token (default: GITLAB_TOKEN)
#   --name NAME          MCP server name (default: gitlab)
#   --global             install at user scope (~/.apm) instead of this project
#   --target LIST        force APM targets, e.g. claude or claude,vscode
#   --if-missing         do nothing if the server is already declared in apm.yml
#   --skip-check         do not probe the GitLab MCP endpoint
#   --dry-run            print the apm command instead of running it
#   -y, --yes            accept defaults for every unanswered question
#   -h, --help           show this help
set -euo pipefail

# Re-entrancy guard: `apm install --mcp` fires post-install lifecycle scripts,
# which may be this wizard again.
if [[ "${HACKATHON_SETUP_RUNNING:-}" == "1" ]]; then exit 0; fi

GITLAB_URL_IN="" AUTH="" TOKEN_VAR="GITLAB_TOKEN" NAME="gitlab"
GLOBAL=0 TARGET="" IF_MISSING=0 SKIP_CHECK=0 DRY_RUN=0 YES=0

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gitlab-url) GITLAB_URL_IN="$2"; shift 2 ;;
    --auth)       AUTH="$2"; shift 2 ;;
    --token-var)  TOKEN_VAR="$2"; shift 2 ;;
    --name)       NAME="$2"; shift 2 ;;
    --global)     GLOBAL=1; shift ;;
    --target)     TARGET="$2"; shift 2 ;;
    --if-missing) IF_MISSING=1; shift ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -y|--yes)     YES=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# ---------- output helpers ----------
if [[ -t 1 ]]; then B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' C=$'\e[36m' N=$'\e[0m'
else B="" G="" Y="" R="" C="" N=""; fi
info() { printf '%s\n' "${C}›${N} $*"; }
ok()   { printf '%s\n' "${G}✔${N} $*"; }
warn() { printf '%s\n' "${Y}!${N} $*" >&2; }
die()  { printf '%s\n' "${R}✖${N} $*" >&2; exit 1; }

# When run as an APM lifecycle script, stdin carries the event JSON, so read
# answers from the terminal instead. Without a terminal, fall back to defaults.
TTY=""
if [[ $YES -eq 0 ]] && (exec </dev/tty) 2>/dev/null; then exec 3</dev/tty; TTY=1; fi

ask() { # ask VAR "Question" "default"
  local __var=$1 __q=$2 __def=$3 __ans=""
  if [[ -n "$TTY" ]]; then
    printf '%s' "${B}?${N} $__q ${__def:+[${__def}] }" >&2
    IFS= read -r __ans <&3 || true
  fi
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

choose() { # choose VAR "Question" default opt1 opt2 ...
  local __var=$1 q=$2 def=$3; shift 3
  local ans
  while :; do
    ask ans "$q ($(IFS=/; echo "$*"))" "$def"
    for o in "$@"; do [[ "$ans" == "$o" ]] && { printf -v "$__var" '%s' "$ans"; return; }; done
    [[ -z "$TTY" ]] && die "Invalid value '$ans' for: $q"
    warn "Choose one of: $*"
  done
}

# ---------- preflight ----------
printf '\n%s\n\n' "${B}hackathon-metrics · MCP setup wizard${N}"

MANIFEST="apm.yml"
[[ $GLOBAL -eq 1 ]] && MANIFEST="${APM_HOME:-$HOME/.apm}/apm.yml"

if [[ -f "$MANIFEST" ]] && grep -Eq "^[[:space:]]*-[[:space:]]*(name:[[:space:]]*)?[\"']?${NAME}[\"']?[[:space:]]*$" "$MANIFEST"; then
  if [[ $IF_MISSING -eq 1 ]]; then
    ok "MCP server '${NAME}' already declared in ${MANIFEST} — nothing to do."
    exit 0
  fi
  warn "MCP server '${NAME}' is already declared in ${MANIFEST}; it will be updated."
fi

APM_BIN="$(command -v apm || true)"
if [[ -z "$APM_BIN" && $DRY_RUN -eq 0 ]]; then
  die "apm CLI not found. Install it first: https://microsoft.github.io/apm/getting-started/installation/"
fi

# ---------- questions ----------
info "GitLab ships a built-in MCP server at /api/v4/mcp (GitLab 18.x+)."
[[ -z "$GITLAB_URL_IN" ]] && ask GITLAB_URL_IN "GitLab base URL" "${GITLAB_URL:-http://localhost:8929}"
BASE="${GITLAB_URL_IN%/}"
BASE="${BASE%/api/v4/mcp}"
[[ "$BASE" =~ ^https?://[^/]+ ]] || die "Invalid URL: '$GITLAB_URL_IN' (expected http(s)://host[:port])"
MCP_URL="${BASE}/api/v4/mcp"

if [[ -z "$AUTH" ]]; then
  info "Auth: 'oauth' = log in through the browser from your agent (recommended; the only"
  info "      option on GitLab CE). 'token' = personal access token with the 'mcp' scope,"
  info "      read from an env var at runtime (GitLab Premium/Ultimate)."
  choose AUTH "Authentication" "oauth" oauth token
fi
case "$AUTH" in
  oauth) ;;
  token)
    [[ "$TOKEN_VAR" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "Invalid env var name: $TOKEN_VAR"
    [[ -n "${!TOKEN_VAR:-}" ]] || warn "\$${TOKEN_VAR} is not set in this shell — export it before starting your agent."
    ;;
  *) die "--auth must be 'oauth' or 'token'" ;;
esac

# ---------- endpoint check ----------
if [[ $SKIP_CHECK -eq 0 ]]; then
  if command -v curl >/dev/null; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' "$MCP_URL" || true)"
    case "$code" in
      000) warn "Could not reach ${MCP_URL}. Is GitLab running? (continuing anyway)" ;;
      404) warn "${MCP_URL} returned 404 — this GitLab may be older than 18.x or have MCP disabled." ;;
      *)   ok "GitLab MCP endpoint answered (HTTP ${code})." ;;
    esac
  else
    warn "curl not found; skipping endpoint check."
  fi
fi
if [[ "$MCP_URL" == http://* && ! "$MCP_URL" =~ ^http://(localhost|127\.|\[::1\]) ]]; then
  warn "Plain http on a non-loopback host: some harnesses (e.g. Codex) only accept https."
fi

# ---------- install ----------
cmd=(apm install --mcp "$NAME" --transport http --url "$MCP_URL")
# The $ is escaped on purpose: APM must store the ${VAR} reference, not the token.
[[ "$AUTH" == "token" ]] && cmd+=(--header "Authorization=Bearer \${${TOKEN_VAR}}")
[[ $GLOBAL -eq 1 ]] && cmd+=(--global)
[[ -n "$TARGET" ]] && cmd+=(--target "$TARGET")

printf '\n'
info "Running: ${B}${cmd[*]}${N}"
if [[ $DRY_RUN -eq 1 ]]; then
  ok "Dry run — nothing written."
  exit 0
fi
HACKATHON_SETUP_RUNNING=1 "${cmd[@]}" </dev/null || die "apm install failed (see output above)."

# ---------- next steps ----------
printf '\n'
ok "MCP server '${NAME}' registered (${MCP_URL})."
printf '\n%s\n' "${B}Next steps${N}"
if [[ "$AUTH" == "oauth" ]]; then
  cat <<EOF
  Claude Code: restart \`claude\`, approve the '${NAME}' server, then run
               /mcp → ${NAME} → Authenticate. Log in to GitLab in the browser
               tab and click Authorize. /mcp should show "${NAME} · connected".
  Other tools: open their MCP panel and complete the OAuth login for '${NAME}'.
EOF
else
  cat <<EOF
  export ${TOKEN_VAR}=<personal access token with the 'mcp' scope>
  Restart your agent (e.g. \`claude\`) and check /mcp shows "${NAME} · connected".
EOF
fi
