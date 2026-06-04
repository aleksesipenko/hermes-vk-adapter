#!/usr/bin/env bash
set -euo pipefail

REPO="${HERMES_VK_REPO:-aleksesipenko/hermes-vk-adapter}"
PLUGIN_NAME="${HERMES_VK_PLUGIN_NAME:-vk}"
PY_DEPS=(vkbottle httpx)

say() {
  printf '\n%s\n' "$*"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'error: required command not found: %s\n' "$1" >&2
    exit 1
  fi
}

hermes_python_from_shebang() {
  local hermes_bin="$1"
  local first_line
  first_line="$(head -n 1 "$hermes_bin" 2>/dev/null || true)"
  case "$first_line" in
    '#!'*)
      printf '%s\n' "${first_line#\#!}"
      ;;
  esac
}

pipx_has_hermes_agent() {
  command -v pipx >/dev/null 2>&1 || return 1
  pipx list --short 2>/dev/null | awk '{print $1}' | grep -qx 'hermes-agent'
}

install_python_deps() {
  say "[1/4] Installing VK Python dependencies in the Hermes runtime"

  if pipx_has_hermes_agent; then
    pipx inject hermes-agent "${PY_DEPS[@]}"
    return
  fi

  local hermes_bin
  hermes_bin="$(command -v hermes)"

  local hermes_python
  hermes_python="$(hermes_python_from_shebang "$hermes_bin")"
  if [ -n "$hermes_python" ] && [ -x "$hermes_python" ]; then
    "$hermes_python" -m pip install --upgrade "${PY_DEPS[@]}"
    return
  fi

  printf 'error: could not locate the Python environment that runs hermes.\n' >&2
  printf 'Install dependencies manually, then rerun:\n' >&2
  printf '  python -m pip install --upgrade %s\n' "${PY_DEPS[*]}" >&2
  exit 1
}

install_plugin() {
  say "[2/4] Installing Hermes VK plugin from ${REPO}"
  local install_args
  install_args=(plugins install "$REPO" --enable)
  if [ -d "${HERMES_HOME:-$HOME/.hermes}/plugins/${PLUGIN_NAME}" ]; then
    install_args+=(--force)
  fi

  if hermes "${install_args[@]}"; then
    return
  fi

  if [ -n "${GITHUB_TOKEN:-}" ]; then
    say "Retrying plugin install without GITHUB_TOKEN"
    env -u GITHUB_TOKEN hermes "${install_args[@]}"
    return
  fi

  exit 1
}

verify_install() {
  say "[3/4] Verifying plugin list"
  hermes plugins list

  say "[4/4] Next gateway checks"
  printf 'Run:\n'
  printf '  hermes gateway status\n'
  printf '  hermes gateway restart\n'
}

main() {
  need_cmd hermes
  need_cmd git

  install_python_deps
  install_plugin
  verify_install
}

main "$@"
