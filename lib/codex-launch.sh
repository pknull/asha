#!/usr/bin/env bash
# Codex TUI launch arguments owned by Asha (sourced by bin/asha and doctor).
#
# Codex 0.157 moved interactive agent threads into a shared background
# `codex app-server --managed-daemon`. Hook subprocesses then run inside that
# daemon and inherit the environment of whichever process first spawned it,
# not the launching pane's, so Control hooks report under a frozen, foreign
# ASHA_HUB_SESSION_ID (#100). `--no-daemon` keeps the thread, and every hook
# it runs, inside this pane's own process.

# Subcommands that are not the interactive TUI. `resume` and `fork` are TUI
# launches and take their own --no-daemon after the subcommand.
ASHA_CODEX_NON_TUI_SUBCOMMANDS=" agents exec e review login logout mcp mcp-server plugin app-server app remote-control completion update doctor sandbox debug apply a queue archive delete migrate-rollouts unarchive cloud cloud-tasks exec-server execpolicy features help responses-api-proxy stdio-to-uds tcp-tunnel "
# Codex 0.157 options that consume the next argument (codex --help and
# codex resume --help); -i/--image consumes every following non-option value.
ASHA_CODEX_VALUE_OPTIONS=" -c --config --enable --disable --remote-auth-token-env -m --model --local-provider -p --profile -s --sandbox -C --cd --add-dir -a --ask-for-approval "

# asha_codex_launch_kind ARGS... -> prints tui, resume (resume/fork first),
# subcommand, or remote. Follows Codex's grammar: option values, `--`, and the
# first positional, which is either a subcommand or the TUI prompt. `--remote`
# anywhere before `--` connects to another server, which refuses --no-daemon.
asha_codex_launch_kind() {
  local kind="" arg
  while [[ $# -gt 0 ]]; do
    arg="$1"
    shift
    case "$arg" in
      --) break ;;
      --remote|--remote=*) echo remote; return 0 ;;
      -i|--image)
        while [[ $# -gt 0 && "$1" != -* ]]; do shift; done ;;
      --*=*) ;;
      -*)
        [[ "$ASHA_CODEX_VALUE_OPTIONS" != *" $arg "* || $# -eq 0 ]] || shift ;;
      *)
        if [[ -z "$kind" ]]; then
          case "$arg" in
            resume|fork) kind=resume ;;
            *) if [[ "$ASHA_CODEX_NON_TUI_SUBCOMMANDS" == *" $arg "* ]]; then kind=subcommand; else kind=tui; fi ;;
          esac
        fi ;;
    esac
  done
  echo "${kind:-tui}"
}

# True when this Codex executable documents --no-daemon (0.157 and later).
# Captured whole rather than piped to grep -q, which under pipefail can turn
# an early grep exit into a SIGPIPE failure of the probe.
asha_codex_supports_no_daemon() {
  local executable="$1" help=""
  if command -v timeout >/dev/null 2>&1; then
    help="$(timeout 5 "$executable" --help </dev/null 2>/dev/null)" || true
  else
    help="$("$executable" --help </dev/null 2>/dev/null)" || true
  fi
  [[ "$help" == *--no-daemon* ]]
}

# asha_codex_launch_args EXECUTABLE ARGS...
# Sets ASHA_CODEX_LAUNCH_ARGS to ARGS with exactly one --no-daemon on a TUI
# launch when Codex supports it, and with none when it does not (an older
# Codex rejects the unknown flag, and has no daemon to escape).
asha_codex_launch_args() {
  local executable="$1" arg present=0 kind
  shift
  ASHA_CODEX_LAUNCH_ARGS=("$@")
  for arg in "$@"; do
    [[ "$arg" != "--" ]] || break
    [[ "$arg" != "--no-daemon" ]] || present=1
  done
  if [[ $present -eq 1 ]]; then
    asha_codex_supports_no_daemon "$executable" && return 0
    # Strip only the option, never prompt text after `--`.
    local seen_separator=0
    ASHA_CODEX_LAUNCH_ARGS=()
    for arg in "$@"; do
      [[ "$arg" != "--" ]] || seen_separator=1
      [[ $seen_separator -eq 0 && "$arg" == "--no-daemon" ]] || ASHA_CODEX_LAUNCH_ARGS+=("$arg")
    done
    return 0
  fi
  kind="$(asha_codex_launch_kind "$@")"
  [[ "$kind" == tui || "$kind" == resume ]] || return 0
  asha_codex_supports_no_daemon "$executable" || return 0
  if [[ "$kind" == resume && ( "${1:-}" == resume || "${1:-}" == fork ) ]]; then
    ASHA_CODEX_LAUNCH_ARGS=("$1" --no-daemon "${@:2}")
  else
    ASHA_CODEX_LAUNCH_ARGS=(--no-daemon "$@")
  fi
}
