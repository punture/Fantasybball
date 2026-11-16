#!/usr/bin/env bash
# Connect FBM to OpenClaw on Linux, macOS or WSL: the `fbm` chat skill plus scheduled Telegram posts that run FBM
# commands with no model call. Safe to re-run: it replaces only what it created (the fbm skill folder and jobs
# named fbm:*), changes OpenClaw settings only through the openclaw CLI, and asks before optional changes.
# Run `openclaw/install.sh --help` for options. Works with the bash 3.2 that ships with macOS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN_COMMAND_JOBS="2026.6.5" # first OpenClaw with command cron jobs (runs a program, no model call)
MIN_CRON_FLAGS="2026.2.17"  # first OpenClaw with --exact on cron jobs (needed by the agent-turn fallback)
JOB_TZ="America/New_York"   # Yahoo NBA game days are US/Eastern: every schedule below is ET
JOB_PREFIX="fbm:"
SKILL_NAME="fbm"
MARKER="fbm-openclaw-skill" # written into the generated SKILL.md; we only replace or remove folders that carry it

# Scheduled jobs: name | cron (ET) | delivery (announce = post stdout to Telegram, none = silent) | timeout s | fbm args
JOBS=(
  "fbm:data-sync|0 3 * * *|none|1800|data sync"
  "fbm:health|30 3 * * *|none|600|health"
  "fbm:snapshot|0 4 * * *|none|600|snapshot"
  "fbm:weekly-retro|0 5 * * 1|none|900|learn retro"
  "fbm:morning-digest|30 8 * * *|announce|600|digest --kind daily"
  "fbm:weekly-preview|0 8 * * 1|announce|600|digest --kind weekly"
  "fbm:lineup-guard|*/15 10-23 * * *|announce|300|alert lineup"
  "fbm:injury-watch|5,20,35,50 10-23 * * *|announce|300|alert injuries"
)
# On OpenClaw older than MIN_COMMAND_JOBS these can run as agent turns instead (one model call per run).
FALLBACK_JOBS=" fbm:morning-digest fbm:weekly-preview "

usage() {
  cat <<'EOF'
Usage: openclaw/install.sh [options]

Installs FBM's `fbm` skill into your OpenClaw workspace (so you can ask "how's my matchup?" in Telegram) and
registers scheduled jobs named fbm:* that run FBM and post its output to your Telegram chat with no model call.

Options:
  --chat-id ID            Telegram chat id for the posts (default: [notify] telegram_chat_id in
                          config/settings.toml, else $FBM_OPENCLAW_CHAT_ID, else a prompt)
  --jobs LIST             Only these jobs, comma-separated (e.g. morning-digest,lineup-guard). Default: all
  --no-jobs               Install the skill only; register no scheduled jobs
  --cron NAME=EXPR        Change one job's schedule (ET, 5-field cron); repeatable.
                          Also: FBM_CRON_<NAME>=EXPR, e.g. FBM_CRON_MORNING_DIGEST="0 9 * * *"
  --fallback              On OpenClaw older than 2026.6.5, schedule the digests as agent turns (uses your model)
  --allowlist | --no-allowlist
                          Add FBM to OpenClaw's exec allowlist when exec isn't in full mode (default: ask)
  --notify-openclaw | --no-notify-openclaw
                          Also add "openclaw" to FBM's [notify] channels (default: ask; answer no keeps settings)
  --no-test-message       Don't send the Telegram test message
  --agent ID              OpenClaw agent for the allowlist and fallback jobs (default: main)
  --skill-dir DIR         Skill folder (default: <OpenClaw workspace>/skills/fbm)
  --repo DIR              FBM checkout (default: the folder above this script)
  --fbm-bin PATH          FBM program (default: <repo>/.venv/bin/fbm)
  --settings FILE         FBM settings file (default: <repo>/config/settings.toml)
  --openclaw PATH         openclaw CLI (default: openclaw on PATH)
  --force                 Replace an existing skill folder named fbm that FBM didn't create
  -y, --yes               No questions: take the suggested answer to each
  --dry-run               Print every command that would change something; change nothing
  --uninstall             Remove the fbm:* jobs, the skill, the allowlist entry and what this script set up
  -h, --help              Show this help
EOF
}

# ------------------------------------------------------------------ options

REPO="" FBM_BIN="" PY="" SKILL_DIR="" SETTINGS="" CHAT_ID="${FBM_OPENCLAW_CHAT_ID:-}"
SELECT_JOBS="" NO_JOBS=0 AGENT="main" FALLBACK="ask" ALLOWLIST="ask" NOTIFY="ask" TEST_MESSAGE=1
ASSUME_YES=0 DRY_RUN=0 UNINSTALL=0 FORCE=0 OPENCLAW="${OPENCLAW_BIN:-openclaw}"
CRON_OVERRIDES=()

need_value() { [ $# -ge 2 ] && [ -n "$2" ] || { echo "error: $1 needs a value (see --help)" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --*=*) opt="${1%%=*}" val="${1#*=}"; shift; set -- "$opt" "$val" "$@"; continue ;;
    --chat-id) need_value "$@"; CHAT_ID="$2"; shift ;;
    --jobs) need_value "$@"; SELECT_JOBS="$2"; shift ;;
    --no-jobs) NO_JOBS=1 ;;
    --cron) need_value "$@"; CRON_OVERRIDES+=("$2"); shift ;;
    --fallback) FALLBACK="yes" ;;
    --no-fallback) FALLBACK="no" ;;
    --allowlist) ALLOWLIST="yes" ;;
    --no-allowlist) ALLOWLIST="no" ;;
    --notify-openclaw) NOTIFY="yes" ;;
    --no-notify-openclaw) NOTIFY="no" ;;
    --no-test-message) TEST_MESSAGE=0 ;;
    --agent) need_value "$@"; AGENT="$2"; shift ;;
    --skill-dir) need_value "$@"; SKILL_DIR="$2"; shift ;;
    --repo) need_value "$@"; REPO="$2"; shift ;;
    --fbm-bin) need_value "$@"; FBM_BIN="$2"; shift ;;
    --settings) need_value "$@"; SETTINGS="$2"; shift ;;
    --openclaw) need_value "$@"; OPENCLAW="$2"; shift ;;
    --force) FORCE=1 ;;
    -y | --yes) ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "error: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if [ -t 0 ] && [ "$ASSUME_YES" = 0 ]; then INTERACTIVE=1; else INTERACTIVE=0; fi
if [ -t 1 ]; then
  BOLD=$'\033[1m' GREEN=$'\033[32m' YELLOW=$'\033[33m' RED=$'\033[31m' RESET=$'\033[0m'
else
  BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi

# ------------------------------------------------------------------ helpers

say() { printf '%s\n' "$*"; }
step() { printf '\n%s%s%s\n' "$BOLD" "$*" "$RESET"; }
ok() { printf '  %sok%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() {
  printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2
  exit 1
}

quote() { # shell-quote arguments for display, so a printed command can be copied
  local out="" arg sq="'"
  for arg in "$@"; do
    case "$arg" in
      "" | *[!A-Za-z0-9_./:=@,+%-]*) arg="$sq${arg//$sq/$sq\\$sq$sq}$sq" ;;
    esac
    out="$out $arg"
  done
  printf '%s' "${out# }"
}

LAST_OUT=""
run() { # a command that changes something: printed under --dry-run, else run with its output kept in LAST_OUT
  LAST_OUT=""
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] %s\n' "$(quote "$@")"
    return 0
  fi
  # stdin from /dev/null: a captured prompt would hang unseen, and a child reading stdin would swallow the rest of a
  # `while read` loop's input (uninstall's job list).
  LAST_OUT="$("$@" 2>&1 </dev/null)"
}

NL=$'\n'
first_line() { printf '%s' "${1%%"$NL"*}"; }

ask() { # ask QUESTION DEFAULT(y|n): --yes takes the default; without a terminal the answer is no
  local answer hint="[y/N]"
  [ "$2" = y ] && hint="[Y/n]"
  if [ "$ASSUME_YES" = 1 ]; then [ "$2" = y ]; return; fi
  if [ "$INTERACTIVE" = 0 ]; then return 1; fi
  read -r -p "  $1 $hint " answer || answer=""
  case "${answer:-$2}" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

decide() { # decide yes|no|ask QUESTION DEFAULT
  case "$1" in yes) return 0 ;; no) return 1 ;; *) ask "$2" "$3" ;; esac
}

json_str() { # a JSON string literal
  local s="${1//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

argv_json() { # a JSON array of strings, for --command-argv
  local out="" arg
  for arg in "$@"; do out="$out,$(json_str "$arg")"; done
  printf '[%s]' "${out#,}"
}

job_selected() { # job_selected NAME
  [ -z "$SELECT_JOBS" ] && return 0
  case ",${SELECT_JOBS// /}," in *",$1,"* | *",${1#"$JOB_PREFIX"},"*) return 0 ;; esac
  return 1
}

known_job() { # known_job NAME (with or without the fbm: prefix)
  local entry
  for entry in "${JOBS[@]}"; do
    case "${entry%%|*}" in "$1" | "$JOB_PREFIX$1") return 0 ;; esac
  done
  return 1
}

check_job_names() { # a typo in --jobs or --cron would otherwise schedule nothing, or keep the default time, silently
  local name item entry all=""
  for entry in "${JOBS[@]}"; do all="$all ${entry%%|*}"; done
  set -f
  for name in $(printf '%s' "$SELECT_JOBS" | tr ',' ' '); do
    known_job "$name" || die "Unknown job '$name' in --jobs. Jobs:$all"
  done
  set +f
  for item in ${CRON_OVERRIDES[@]+"${CRON_OVERRIDES[@]}"}; do
    case "$item" in *=?*) ;; *) die "--cron needs NAME=EXPR (e.g. morning-digest=\"0 9 * * *\"), got '$item'." ;; esac
    known_job "${item%%=*}" || die "Unknown job '${item%%=*}' in --cron. Jobs:$all"
  done
}

job_schedule() { # job_schedule NAME DEFAULT -> --cron override, else FBM_CRON_<NAME>, else DEFAULT
  local short="${1#"$JOB_PREFIX"}" item key value
  for item in ${CRON_OVERRIDES[@]+"${CRON_OVERRIDES[@]}"}; do
    case "$item" in "$1="* | "$short="*) say "${item#*=}"; return ;; esac
  done
  key="FBM_CRON_$(printf '%s' "$short" | tr 'a-z-' 'A-Z_')"
  value="${!key:-}"
  say "${value:-$2}"
}

# ------------------------------------------------------------------ steps

locate_fbm() {
  step "FBM"
  REPO="$(cd "${REPO:-$SCRIPT_DIR/..}" && pwd)" || die "FBM folder not found."
  [ -f "$REPO/pyproject.toml" ] && [ -d "$REPO/agents/skills" ] ||
    die "$REPO doesn't look like an FBM checkout (use --repo DIR)."
  HELPER="$REPO/scripts/render_openclaw_skill.py"
  [ -f "$HELPER" ] || die "$HELPER is missing: update this FBM checkout (git pull)."
  SETTINGS="${SETTINGS:-$REPO/config/settings.toml}"
  STATE_FILE="$REPO/data/openclaw/install-state"
  if [ -z "$FBM_BIN" ]; then
    FBM_BIN="$REPO/.venv/bin/fbm"
    if [ ! -x "$FBM_BIN" ] && [ "$UNINSTALL" = 0 ]; then
      command -v uv >/dev/null 2>&1 ||
        die "FBM isn't installed yet and uv isn't on PATH. Install uv (https://docs.astral.sh/uv/), run 'uv sync' in $REPO, then run this again."
      say "  Setting up FBM's Python environment (uv sync)..."
      if [ "$DRY_RUN" = 1 ]; then
        printf '  [dry-run] cd %s && uv sync\n' "$(quote "$REPO")"
      else
        (cd "$REPO" && uv sync) || die "uv sync failed in $REPO."
      fi
    fi
  fi
  case "$FBM_BIN" in /*) ;; *) FBM_BIN="$PWD/$FBM_BIN" ;; esac
  if [ -x "$REPO/.venv/bin/python" ]; then PY="$REPO/.venv/bin/python"; else PY="$(command -v python3 || true)"; fi
  [ -n "$PY" ] || die "No Python found to run $HELPER (expected $REPO/.venv/bin/python)."
  [ "$UNINSTALL" = 1 ] && return 0
  if [ ! -x "$FBM_BIN" ]; then
    [ "$DRY_RUN" = 1 ] || die "FBM program not found at $FBM_BIN (use --fbm-bin PATH)."
    warn "FBM program not found at $FBM_BIN yet (dry run)."
    return 0
  fi
  local tools
  tools="$("$FBM_BIN" tool --list 2>&1)" || die "FBM didn't start: $(first_line "$tools")"
  case "$tools" in *context_pack*) ;; *) die "'$FBM_BIN tool --list' didn't list FBM's tools." ;; esac
  ok "FBM at $FBM_BIN"
}

locate_openclaw() {
  step "OpenClaw"
  OC_FOUND=0 COMMAND_JOBS=1 CRON_FLAGS=1
  if ! command -v "$OPENCLAW" >/dev/null 2>&1; then
    [ "$DRY_RUN" = 1 ] || [ "$UNINSTALL" = 1 ] ||
      die "The openclaw CLI isn't on PATH. Install OpenClaw (https://openclaw.ai) and connect Telegram, then run this again."
    warn "The openclaw CLI isn't on PATH; continuing without it."
    return 0
  fi
  OPENCLAW="$(command -v "$OPENCLAW")"
  case "$OPENCLAW" in /*) ;; *) OPENCLAW="$PWD/$OPENCLAW" ;; esac # --openclaw ./x: settings need an absolute path
  OC_FOUND=1
  [ "$UNINSTALL" = 1 ] && return 0
  local text version rc=0
  text="$("$OPENCLAW" --version 2>&1 || true)"
  version="$("$PY" "$HELPER" version "$text" --min "$MIN_COMMAND_JOBS")" || rc=$?
  case "$rc" in
    0) ok "OpenClaw $version" ;;
    1)
      COMMAND_JOBS=0
      warn "OpenClaw $version is older than $MIN_COMMAND_JOBS, the first version that runs scheduled commands without a model call. Update it with: openclaw update"
      "$PY" "$HELPER" version "$text" --min "$MIN_CRON_FLAGS" >/dev/null || CRON_FLAGS=0
      ;;
    *) warn "Couldn't read the OpenClaw version ($(first_line "$text")); assuming $MIN_COMMAND_JOBS or newer." ;;
  esac
  if "$OPENCLAW" gateway status --require-rpc >/dev/null 2>&1 || "$OPENCLAW" health >/dev/null 2>&1; then
    ok "Gateway is running"
  elif [ "$DRY_RUN" = 1 ]; then
    warn "The OpenClaw Gateway isn't reachable (dry run continues)."
  else
    die "The OpenClaw Gateway isn't reachable. Start it ('openclaw gateway start', or open the OpenClaw app) and run this again."
  fi
}

load_state() {
  STATE_FAILURE_ALERT=0 STATE_ALLOWLIST=0 STATE_NOTIFY=0 STATE_CHAT_ID="" STATE_SKILL_DIR="" STATE_FBM_BIN=""
  [ -f "$STATE_FILE" ] || return 0
  STATE_FAILURE_ALERT="$(sed -n 's/^failure_alert=//p' "$STATE_FILE")"
  STATE_ALLOWLIST="$(sed -n 's/^allowlist=//p' "$STATE_FILE")"
  STATE_NOTIFY="$(sed -n 's/^notify_channel=//p' "$STATE_FILE")"
  STATE_CHAT_ID="$(sed -n 's/^chat_id=//p' "$STATE_FILE")"
  STATE_SKILL_DIR="$(sed -n 's/^skill_dir=//p' "$STATE_FILE")"
  STATE_FBM_BIN="$(sed -n 's/^fbm_bin=//p' "$STATE_FILE")"
  [ -n "$SKILL_DIR" ] || SKILL_DIR="$STATE_SKILL_DIR"
}

save_state() {
  [ "$DRY_RUN" = 1 ] && return 0
  mkdir -p "$(dirname "$STATE_FILE")"
  {
    say "# Written by openclaw/install.sh so --uninstall removes exactly what it set up."
    say "skill_dir=$SKILL_DIR"
    say "fbm_bin=$FBM_BIN"
    say "chat_id=$CHAT_ID"
    say "failure_alert=${STATE_FAILURE_ALERT:-0}"
    say "allowlist=${STATE_ALLOWLIST:-0}"
    say "notify_channel=${STATE_NOTIFY:-0}"
  } >"$STATE_FILE"
}

resolve_chat_id() {
  step "Telegram"
  local value="$CHAT_ID"
  [ -n "$value" ] || value="$("$PY" "$HELPER" chat-id --settings "$SETTINGS" 2>/dev/null || true)"
  if [ -z "$value" ] && [ "$INTERACTIVE" = 1 ]; then
    say "  Your chat id: send /whoami to your OpenClaw bot in Telegram (for a direct chat it's your user id)."
    read -r -p "  Telegram chat id: " value || value=""
  fi
  CHAT_ID="$("$PY" "$HELPER" chat-id --value "$value" --settings /dev/null 2>/dev/null)" ||
    die "A Telegram chat id is needed for the posts: pass --chat-id ID or set [notify] telegram_chat_id in $SETTINGS."
  ok "Posts go to Telegram chat $CHAT_ID"
}

resolve_skill_dir() {
  [ -n "$SKILL_DIR" ] && return 0
  local ws="${OPENCLAW_WORKSPACE_DIR:-}"
  if [ -z "$ws" ] && [ "$OC_FOUND" = 1 ]; then
    ws="$("$OPENCLAW" config get "agents.entries.$AGENT.workspace" 2>/dev/null || true)"
    [ -n "$ws" ] || ws="$("$OPENCLAW" config get agents.defaults.workspace 2>/dev/null || true)"
    ws="$(first_line "$ws")"
  fi
  if [ -z "$ws" ]; then
    if [ -n "${OPENCLAW_STATE_DIR:-}" ]; then
      ws="$OPENCLAW_STATE_DIR/workspace"
    elif [ -n "${OPENCLAW_PROFILE:-}" ] && [ "$OPENCLAW_PROFILE" != default ]; then
      ws="$HOME/.openclaw-$OPENCLAW_PROFILE/workspace"
    else
      ws="$HOME/.openclaw/workspace"
    fi
  fi
  case "$ws" in \~ | \~/*) ws="$HOME${ws#\~}" ;; esac # a literal ~ from openclaw.json
  SKILL_DIR="$ws/skills/$SKILL_NAME"
}

install_skill() {
  step "Chat skill"
  resolve_skill_dir
  local args=(render --bin "$FBM_BIN" --out "$SKILL_DIR" --repo "$REPO")
  [ "$FORCE" = 1 ] && args+=(--force)
  if ! run "$PY" "$HELPER" "${args[@]}"; then
    die "Couldn't write the skill: $LAST_OUT"
  fi
  ok "Skill 'fbm' in $SKILL_DIR (with FBM's playbooks in references/)"
  if [ "$DRY_RUN" = 1 ] || [ "$OC_FOUND" = 0 ]; then return 0; fi
  if "$OPENCLAW" skills info "$SKILL_NAME" >/dev/null 2>&1; then
    ok "OpenClaw sees the skill (start a new chat with /new if it's open)"
  else
    warn "OpenClaw doesn't list the 'fbm' skill yet. Start a new chat (/new) or restart the Gateway; if it's still missing, re-run with --skill-dir <your agent workspace>/skills/fbm."
  fi
}

send_test_message() {
  [ "$TEST_MESSAGE" = 1 ] && [ "$OC_FOUND" = 1 ] || return 0
  if run "$OPENCLAW" message send --channel telegram --target "$CHAT_ID" \
    --message "FBM is connected to OpenClaw. Scheduled posts arrive in this chat; reply here to ask about your league."; then
    ok "Test message sent to Telegram"
  else
    warn "The Telegram test message failed: $(first_line "$LAST_OUT"). Check 'openclaw channels status' and the chat id."
  fi
}

ensure_failure_alert() {
  step "Failure alerts"
  if [ "$OC_FOUND" = 1 ] && "$OPENCLAW" config get cron.failureAlert --json >/dev/null 2>&1; then
    if [ "$(first_line "$("$OPENCLAW" config get cron.failureAlert.enabled 2>/dev/null || true)")" = false ]; then
      warn "cron.failureAlert is set but disabled (enabled: false): a failing fbm:health, fbm:snapshot or fbm:data-sync won't alert you. Left unchanged."
    else
      ok "cron.failureAlert is already set; left unchanged"
    fi
    return 0
  fi
  local patch
  patch="$(mktemp "${TMPDIR:-/tmp}/fbm-openclaw-patch.XXXXXX")"
  printf '{"cron":{"failureAlert":{"enabled":true,"mode":"announce","channel":"telegram","to":%s}}}\n' \
    "$(json_str "$CHAT_ID")" >"$patch"
  [ "$DRY_RUN" = 1 ] && say "  patch file: $(cat "$patch")"
  if run "$OPENCLAW" config patch --file "$patch"; then
    [ "$DRY_RUN" = 1 ] || STATE_FAILURE_ALERT=1
    ok "A failed job (e.g. fbm health) now alerts Telegram chat $CHAT_ID (cron.failureAlert)"
  else
    warn "Couldn't set cron.failureAlert: $(first_line "$LAST_OUT")"
  fi
  rm -f "$patch"
}

check_exec_policy() {
  step "Chat commands (exec policy)"
  [ "$OC_FOUND" = 1 ] || return 0
  local mode
  mode="$("$OPENCLAW" config get "agents.entries.$AGENT.tools.exec.mode" 2>/dev/null || true)"
  [ -n "$mode" ] || mode="$("$OPENCLAW" config get tools.exec.mode 2>/dev/null || true)"
  mode="$(first_line "$mode")"
  case "${mode:-full}" in
    full) ok "exec mode is full: the agent can run FBM without approval prompts" ;;
    deny) warn "exec is denied (tools.exec.mode=deny): the agent can't run FBM in chat. Scheduled posts still work." ;;
    *)
      if decide "$ALLOWLIST" "exec mode is '$mode'. Add FBM to the exec allowlist so chat questions run without prompts?" y; then
        if run "$OPENCLAW" approvals allowlist add --agent "$AGENT" "$FBM_BIN"; then
          [ "$DRY_RUN" = 1 ] || STATE_ALLOWLIST=1
          ok "FBM is on the exec allowlist for agent '$AGENT'"
        else
          warn "Couldn't update the allowlist: $(first_line "$LAST_OUT")"
        fi
      else
        warn "Skipped. To allow it later: openclaw approvals allowlist add --agent $AGENT $(quote "$FBM_BIN")"
      fi
      ;;
  esac
}

load_existing_jobs() {
  EXISTING=""
  [ "$OC_FOUND" = 1 ] || return 0
  local json
  json="$("$OPENCLAW" cron list --all --json 2>/dev/null)" || json="$("$OPENCLAW" cron list --json 2>/dev/null)" || {
    warn "Couldn't list existing OpenClaw jobs; same-named fbm:* jobs may be duplicated."
    return 0
  }
  EXISTING="$(printf '%s' "$json" | "$PY" "$HELPER" job-ids --prefix "$JOB_PREFIX")" || EXISTING=""
}

ids_named() { printf '%s\n' "$EXISTING" | awk -F '\t' -v name="$1" '$2 == name { print $1 }'; }

HAS_CMD="|" LACKS_CMD="|"
fbm_has() { # fbm_has WORDS...: does this FBM have that subcommand? (older FBM versions lack digest/alert)
  [ -x "$FBM_BIN" ] || return 0
  case "$HAS_CMD" in *"|$*|"*) return 0 ;; esac
  case "$LACKS_CMD" in *"|$*|"*) return 1 ;; esac
  if "$FBM_BIN" "$@" --help >/dev/null 2>&1; then HAS_CMD="$HAS_CMD$*|"; else LACKS_CMD="$LACKS_CMD$*|" && return 1; fi
}

job_command_words() { # "digest --kind daily" -> "digest" ; "alert lineup" -> "alert lineup"
  local word out=""
  for word in $1; do
    case "$word" in -*) break ;; esac
    out="$out $word"
  done
  say "${out# }"
}

register_jobs() {
  step "Scheduled posts"
  REGISTERED="" SKIPPED=""
  if [ "$NO_JOBS" = 1 ]; then
    ok "Skipped (--no-jobs)"
    return 0
  fi
  local use_fallback=0
  if [ "$COMMAND_JOBS" = 0 ]; then
    if [ "$CRON_FLAGS" = 1 ] && decide "$FALLBACK" "Schedule the morning digest and weekly preview as agent turns instead (each run is one model call)?" n; then
      use_fallback=1
    else
      warn "No jobs scheduled: update OpenClaw ('openclaw update') and run this again. Meanwhile FBM's own scheduler can run data sync and health: $(quote "$FBM_BIN") automation install --mode engine-only --jobs data-sync,health"
      return 0
    fi
  fi
  load_existing_jobs
  local entry name expr delivery timeout fbm_args words old_ids id
  local -a argv cmd
  for entry in "${JOBS[@]}"; do
    IFS='|' read -r name expr delivery timeout fbm_args <<<"$entry"
    job_selected "$name" || continue
    expr="$(job_schedule "$name" "$expr")"
    words="$(job_command_words "$fbm_args")"
    # shellcheck disable=SC2086 # split the subcommand words on purpose
    if ! fbm_has $words; then
      warn "$name skipped: this FBM has no 'fbm $words' yet. Update FBM (git pull, uv sync) and run this again."
      SKIPPED="$SKIPPED $name"
      continue
    fi
    if [ "$use_fallback" = 1 ]; then
      case "$FALLBACK_JOBS" in *" $name "*) ;; *)
        SKIPPED="$SKIPPED $name"
        continue
        ;;
      esac
      cmd=("$OPENCLAW" cron add --name "$name" --cron "$expr" --tz "$JOB_TZ" --exact --agent "$AGENT" --session isolated
        --message "Scheduled FBM post. Run exactly this once with exec: $(quote "$FBM_BIN") $fbm_args - then reply with exactly what it printed and nothing else. If it printed only NO_REPLY, reply exactly NO_REPLY."
        --announce --channel telegram --to "$CHAT_ID")
    else
      read -r -a argv <<<"$fbm_args"
      cmd=("$OPENCLAW" cron add --name "$name" --cron "$expr" --tz "$JOB_TZ" --exact
        --command-argv "$(argv_json "$FBM_BIN" "${argv[@]}")" --command-cwd "$REPO" --timeout-seconds "$timeout")
      if [ "$delivery" = announce ]; then
        cmd+=(--announce --channel telegram --to "$CHAT_ID")
      else
        cmd+=(--no-deliver)
      fi
    fi
    old_ids="$(ids_named "$name")"
    if ! run "${cmd[@]}"; then
      warn "$name: couldn't register it: $(first_line "$LAST_OUT")"
      SKIPPED="$SKIPPED $name"
      continue
    fi
    for id in $old_ids; do # replace the previous version only once the new one exists
      run "$OPENCLAW" cron remove "$id" || warn "$name: couldn't remove the old job $id: $(first_line "$LAST_OUT")"
    done
    REGISTERED="$REGISTERED $name"
    ok "$name  $expr (ET)  ->  fbm $fbm_args$([ "$delivery" = announce ] && printf '  (posts to Telegram)')"
  done
}

maybe_enable_notify() {
  step "FBM's own notifications"
  if decide "$NOTIFY" "Also let FBM send its own alerts (fbm ... --send) through OpenClaw? This adds \"openclaw\" to [notify] channels in config/settings.toml." n; then
    if run "$PY" "$HELPER" notify-channel --settings "$SETTINGS" --add openclaw --chat-id "$CHAT_ID" --openclaw-bin "$OPENCLAW"; then
      [ "$DRY_RUN" = 1 ] || STATE_NOTIFY=1
      ok "${LAST_OUT:-[notify] channels updated} (backup: settings.toml.bak)"
    else
      warn "Couldn't update $SETTINGS: $(first_line "$LAST_OUT")"
    fi
  else
    ok "Left config/settings.toml unchanged (the scheduled posts don't need it)"
  fi
}

check_fbm_scheduler() {
  [ -x "$FBM_BIN" ] || return 0
  local out
  out="$("$FBM_BIN" automation list 2>/dev/null || true)"
  case "$out" in "" | *"No FBM scheduler entries"*) return 0 ;; esac
  warn "FBM's own OS scheduler also has jobs ('fbm automation list'), so digests and alerts would arrive twice. OpenClaw now runs all of that work: remove them and keep 'fbm setup' from installing them again: $(quote "$FBM_BIN") automation remove && $(quote "$FBM_BIN") setup --automation none --skip-data --yes"
}

summary() {
  step "Done"
  [ "$DRY_RUN" = 1 ] && say "  Dry run: nothing was changed."
  [ -n "${REGISTERED:-}" ] && say "  Jobs:${REGISTERED}  (times are US/Eastern; list them: openclaw cron list)"
  [ -n "${SKIPPED:-}" ] && say "  Not scheduled:${SKIPPED}"
  say "  Try it in Telegram: \"how's my matchup?\", \"set today's lineup\", \"any pickups for steals?\""
  say "  Run a job now: openclaw cron run <job-id>   History: openclaw cron runs <job-id>"
  say "  Remove everything: $(quote "$SCRIPT_DIR/install.sh") --uninstall"
}

uninstall() {
  step "Uninstall"
  load_state
  resolve_skill_dir
  local id name
  if [ "$OC_FOUND" = 1 ]; then
    load_existing_jobs
    [ -n "$EXISTING" ] || ok "No fbm:* jobs found in OpenClaw"
    while IFS="$(printf '\t')" read -r id name; do
      [ -n "$id" ] || continue
      if run "$OPENCLAW" cron remove "$id"; then ok "Removed job $name"; else warn "Couldn't remove $name ($id): $(first_line "$LAST_OUT")"; fi
    done <<<"$EXISTING"
    if run "$OPENCLAW" approvals allowlist remove --agent "$AGENT" "${STATE_FBM_BIN:-$FBM_BIN}"; then
      ok "FBM is off the exec allowlist (if it was there)"
    fi
    if [ "$STATE_FAILURE_ALERT" = 1 ]; then
      local to
      to="$(first_line "$("$OPENCLAW" config get cron.failureAlert.to 2>/dev/null || true)")"
      if [ "$to" = "$STATE_CHAT_ID" ] && run "$OPENCLAW" config unset cron.failureAlert; then
        ok "Removed the cron.failureAlert this script set"
      else
        warn "cron.failureAlert was changed since install; left as is"
      fi
    fi
  else
    warn "openclaw CLI not found: remove the fbm:* jobs yourself (openclaw cron list, openclaw cron remove <id>)."
  fi
  if [ -f "$SKILL_DIR/SKILL.md" ] && grep -q "$MARKER" "$SKILL_DIR/SKILL.md"; then
    run rm -rf "$SKILL_DIR" && ok "Removed the skill folder $SKILL_DIR"
  elif [ -e "$SKILL_DIR" ]; then
    warn "$SKILL_DIR wasn't created by FBM; left as is"
  else
    ok "No skill folder at $SKILL_DIR"
  fi
  if [ "$STATE_NOTIFY" = 1 ] && run "$PY" "$HELPER" notify-channel --settings "$SETTINGS" --remove openclaw; then
    ok "Removed \"openclaw\" from FBM's [notify] channels"
  fi
  [ "$DRY_RUN" = 1 ] || rm -f "$STATE_FILE"
  step "Done"
  [ "$DRY_RUN" = 1 ] && say "  Dry run: nothing was changed."
  say "  FBM itself is untouched; run openclaw/install.sh to connect it again."
}

main() {
  say "${BOLD}FBM x OpenClaw${RESET}$([ "$DRY_RUN" = 1 ] && printf ' (dry run)')"
  check_job_names
  locate_fbm
  locate_openclaw
  if [ "$UNINSTALL" = 1 ]; then
    uninstall
    return
  fi
  load_state
  resolve_chat_id
  install_skill
  send_test_message
  ensure_failure_alert
  check_exec_policy
  register_jobs
  maybe_enable_notify
  check_fbm_scheduler
  save_state
  summary
}

main
