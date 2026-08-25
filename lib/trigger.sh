#!/usr/bin/env bash
# asha trigger: scheduled coordinator launches via systemd user timers.
# source-scoped library: bin/asha sources it and calls asha_trigger_main.
#
# A trigger is a pair of user units, asha-trigger-NAME.{service,timer}, whose
# service runs `asha initiative coordinator launch --root DIR --intent TEXT`.
# A fired run stops at plan approval like every other initiative: triggers
# schedule proposals, never unattended execution. Only units carrying the
# managed marker are ever modified or removed.

ASHA_TRIGGER_MARKER="# Managed by asha trigger; edit via 'asha trigger', not by hand."

asha_trigger_usage() {
  cat <<'USAGE'
Usage:
  asha trigger add NAME --schedule CALENDAR --root DIR --intent TEXT
                   [--harness H] [--dry-run]
  asha trigger list
  asha trigger remove NAME [--dry-run]

  NAME       lowercase slug (a-z, 0-9, dashes), max 40 chars
  CALENDAR   systemd OnCalendar expression, e.g. "Mon..Fri 07:03"
  A fired trigger launches a coordinator session whose proposal waits for
  plan approval; nothing runs unattended past that boundary.
USAGE
}

_trigger_unit_dir() { echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; }

_trigger_valid_name() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]]; }

_trigger_owned() { # unit-path
  [[ -f "$1" ]] && sed -n 2p "$1" | grep -qF "$ASHA_TRIGGER_MARKER"
}

asha_trigger_main() {
  local root="${ASHA_ROOT:-}"
  if [[ -z "$root" ]]; then
    echo "ERROR: ASHA_ROOT is not set (lib/trigger.sh is sourced by bin/asha only)" >&2
    return 2
  fi
  local verb="${1:-}"
  shift || true
  case "$verb" in
    add) _trigger_add "$root" "$@" ;;
    list) _trigger_list ;;
    remove) _trigger_remove "$@" ;;
    -h|--help|help) asha_trigger_usage ;;
    "") asha_trigger_usage >&2; return 2 ;;
    *) echo "asha trigger: unknown verb: $verb" >&2; asha_trigger_usage >&2; return 2 ;;
  esac
}

_trigger_add() {
  local asha_root="$1"
  shift
  local name="" schedule="" project_root="" intent="" harness="claude" dry=0
  while (($#)); do
    case "$1" in
      --schedule) schedule="${2:-}"; shift ;;
      --root) project_root="${2:-}"; shift ;;
      --intent) intent="${2:-}"; shift ;;
      --harness) harness="${2:-}"; shift ;;
      --dry-run) dry=1 ;;
      -*) echo "asha trigger add: unknown option: $1" >&2; return 2 ;;
      *) if [[ -n "$name" ]]; then echo "asha trigger add: one NAME only" >&2; return 2; fi
         name="$1" ;;
    esac
    shift
  done
  if [[ -z "$name" || -z "$schedule" || -z "$project_root" || -z "$intent" ]]; then
    echo "asha trigger add: NAME, --schedule, --root, and --intent are required" >&2
    return 2
  fi
  if ! _trigger_valid_name "$name"; then
    echo "asha trigger add: NAME must be a lowercase slug (a-z, 0-9, dashes)" >&2
    return 2
  fi
  if ! project_root="$(cd -P -- "$project_root" 2>/dev/null && pwd)"; then
    echo "asha trigger add: --root directory not found" >&2
    return 2
  fi
  if [[ "$intent" == *$'\n'* || "${#intent}" -gt 2000 ]]; then
    echo "asha trigger add: --intent must be one line under 2000 characters" >&2
    return 2
  fi
  if command -v systemd-analyze >/dev/null 2>&1; then
    if ! systemd-analyze calendar "$schedule" >/dev/null 2>&1; then
      echo "asha trigger add: --schedule is not a valid OnCalendar expression" >&2
      return 2
    fi
  fi
  local unit_dir
  unit_dir="$(_trigger_unit_dir)"
  local service="$unit_dir/asha-trigger-$name.service"
  local timer="$unit_dir/asha-trigger-$name.timer"
  local unit
  for unit in "$service" "$timer"; do
    if [[ -e "$unit" ]] && ! _trigger_owned "$unit"; then
      echo "asha trigger add: foreign unit exists at $unit; refusing" >&2
      return 2
    fi
  done
  local escaped_intent="${intent//\"/\\\"}"
  local asha_home_env=""
  if [[ -n "${ASHA_HOME:-}" && "${ASHA_HOME}" != "$HOME/.asha" ]]; then
    # A non-default root must reach the fired coordinator, which runs outside
    # any shell that exported it.
    asha_home_env="
Environment=\"ASHA_HOME=${ASHA_HOME}\""
  fi
  local service_body="[Unit]
$ASHA_TRIGGER_MARKER
Description=asha trigger $name: coordinator launch

[Service]
Type=oneshot
Environment=\"PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin\"${asha_home_env}
ExecStart=$asha_root/bin/asha initiative coordinator launch --root \"$project_root\" --harness \"$harness\" --intent \"$escaped_intent\"
"
  local timer_body="[Unit]
$ASHA_TRIGGER_MARKER
Description=asha trigger $name: schedule

[Timer]
OnCalendar=$schedule
Persistent=true

[Install]
WantedBy=timers.target
"
  if ((dry)); then
    echo "would write $service:"
    printf '%s' "$service_body"
    echo "would write $timer:"
    printf '%s' "$timer_body"
    echo "would run: systemctl --user daemon-reload"
    echo "would run: systemctl --user enable --now asha-trigger-$name.timer"
    return 0
  fi
  mkdir -p "$unit_dir"
  printf '%s' "$service_body" > "$service"
  printf '%s' "$timer_body" > "$timer"
  systemctl --user daemon-reload
  systemctl --user enable --now "asha-trigger-$name.timer"
  if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze calendar "$schedule" 2>/dev/null | grep -E "Next elapse|From now" || true
  fi
  echo "Trigger $name armed; fired runs wait at plan approval in asha control."
}

_trigger_list() {
  local unit_dir
  unit_dir="$(_trigger_unit_dir)"
  local found=0 timer name intent
  for timer in "$unit_dir"/asha-trigger-*.timer; do
    [[ -f "$timer" ]] || continue
    _trigger_owned "$timer" || continue
    found=1
    name="$(basename "$timer" .timer)"
    name="${name#asha-trigger-}"
    intent="$(grep -o -- '--intent ".*"' "$unit_dir/asha-trigger-$name.service" 2>/dev/null | head -1)"
    printf '%-20s %s\n' "$name" "$(grep '^OnCalendar=' "$timer" | cut -d= -f2-)"
    printf '%-20s %s\n' "" "${intent:-intent: ?}"
  done
  if ((found)) && command -v systemctl >/dev/null 2>&1; then
    systemctl --user list-timers 'asha-trigger-*' --no-pager 2>/dev/null | head -12 || true
  fi
  ((found)) || echo "No asha triggers."
}

_trigger_remove() {
  local name="" dry=0
  while (($#)); do
    case "$1" in
      --dry-run) dry=1 ;;
      -*) echo "asha trigger remove: unknown option: $1" >&2; return 2 ;;
      *) name="$1" ;;
    esac
    shift
  done
  if [[ -z "$name" ]] || ! _trigger_valid_name "$name"; then
    echo "asha trigger remove: NAME (lowercase slug) is required" >&2
    return 2
  fi
  local unit_dir
  unit_dir="$(_trigger_unit_dir)"
  local service="$unit_dir/asha-trigger-$name.service"
  local timer="$unit_dir/asha-trigger-$name.timer"
  if [[ ! -e "$timer" && ! -e "$service" ]]; then
    echo "asha trigger remove: no trigger named $name" >&2
    return 2
  fi
  local unit
  for unit in "$service" "$timer"; do
    if [[ -e "$unit" ]] && ! _trigger_owned "$unit"; then
      echo "asha trigger remove: $unit lacks the managed marker; refusing" >&2
      return 2
    fi
  done
  if ((dry)); then
    echo "would run: systemctl --user disable --now asha-trigger-$name.timer"
    echo "would remove: $timer"
    echo "would remove: $service"
    return 0
  fi
  systemctl --user disable --now "asha-trigger-$name.timer" 2>/dev/null || true
  local removable
  for removable in "$timer" "$service"; do
    [[ -e "$removable" ]] && command rm -- "$removable"
  done
  systemctl --user daemon-reload
  echo "Trigger $name removed."
}
