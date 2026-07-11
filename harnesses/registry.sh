#!/usr/bin/env bash
# Central catalogue for harness identity, homes, executables, and target sets.
# Keep launch-specific behavior in bin/asha and native install behavior in each
# harness adapter; this file owns only the data shared across lifecycle phases.

asha_harnesses() {
  printf '%s\n' claude codex copilot opencode
}

asha_harness_exists() {
  case "${1:-}" in claude|codex|copilot|opencode) return 0 ;; *) return 1 ;; esac
}

asha_target_exists() {
  asha_harness_exists "${1:-}" && return 0
  case "${1:-}" in both|all) return 0 ;; *) return 1 ;; esac
}

asha_expand_target() {
  case "${1:-}" in
    claude|codex|copilot|opencode) printf '%s\n' "$1" ;;
    both) printf '%s\n' claude codex ;;
    all) asha_harnesses ;;
    *) return 1 ;;
  esac
}

asha_harness_home() {
  case "${1:-}" in
    claude) printf '%s\n' "${CLAUDE_HOME:-$HOME/.claude}" ;;
    codex) printf '%s\n' "${CODEX_HOME:-$HOME/.codex}" ;;
    copilot) printf '%s\n' "${COPILOT_HOME:-$HOME/.copilot}" ;;
    opencode) printf '%s\n' "${ASHA_OPENCODE_HOME:-${OPENCODE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}}" ;;
    *) return 1 ;;
  esac
}

asha_harness_executable() {
  case "${1:-}" in
    claude) printf '%s\n' "${ASHA_CLAUDE_CMD:-claude}" ;;
    codex) printf '%s\n' "${ASHA_CODEX_CMD:-codex}" ;;
    copilot) printf '%s\n' "${ASHA_COPILOT_CMD:-copilot}" ;;
    opencode) printf '%s\n' "${ASHA_OPENCODE_CMD:-opencode}" ;;
    *) return 1 ;;
  esac
}

asha_harness_native_config() {
  local home
  home="$(asha_harness_home "$1")" || return 1
  case "$1" in
    claude) printf '%s\n' "${CLAUDE_SETTINGS:-$home/settings.json}" ;;
    codex) printf '%s\n' "$home/config.toml" ;;
    copilot) printf '%s\n' "$home/hooks/asha-guardrails.json" ;;
    opencode) printf '%s\n' "$home/opencode.json" ;;
  esac
}

asha_harness_requires_native_config() {
  case "${1:-}" in claude|codex) return 0 ;; copilot|opencode) return 1 ;; *) return 1 ;; esac
}

asha_harness_shims() {
  local h
  while IFS= read -r h; do printf 'asha-%s\n' "$h"; done < <(asha_harnesses)
}

asha_harness_names_inline() {
  local out="" h
  while IFS= read -r h; do out="${out:+$out|}$h"; done < <(asha_harnesses)
  printf '%s\n' "$out"
}
