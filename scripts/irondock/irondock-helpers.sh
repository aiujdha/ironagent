#!/usr/bin/env bash
# IronDock - Docker helpers for IronAgent
# Inspired by Simon Willison's "Running IronAgent in Docker"
# https://til.simonwillison.net/llms/ironagent-docker
#
# Installation:
#   mkdir -p ~/.irondock && curl -sL https://raw.githubusercontent.com/ironagent/ironagent/main/scripts/irondock/irondock-helpers.sh -o ~/.irondock/irondock-helpers.sh
#   echo 'source ~/.irondock/irondock-helpers.sh' >> ~/.zshrc
#
# Usage:
#   irondock-help    # Show all available commands

# =============================================================================
# Colors
# =============================================================================
_CLR_RESET='\033[0m'
_CLR_BOLD='\033[1m'
_CLR_DIM='\033[2m'
_CLR_GREEN='\033[0;32m'
_CLR_YELLOW='\033[1;33m'
_CLR_BLUE='\033[0;34m'
_CLR_MAGENTA='\033[0;35m'
_CLR_CYAN='\033[0;36m'
_CLR_RED='\033[0;31m'

# Styled command output (green + bold)
_clr_cmd() {
  echo -e "${_CLR_GREEN}${_CLR_BOLD}$1${_CLR_RESET}"
}

# Inline command for use in sentences
_cmd() {
  echo "${_CLR_GREEN}${_CLR_BOLD}$1${_CLR_RESET}"
}

# =============================================================================
# Config
# =============================================================================
IRONDOCK_CONFIG="${HOME}/.irondock/config"

# Common paths to check for IronAgent
IRONDOCK_COMMON_PATHS=(
  "${HOME}/ironagent"
  "${HOME}/workspace/ironagent"
  "${HOME}/projects/ironagent"
  "${HOME}/dev/ironagent"
  "${HOME}/code/ironagent"
  "${HOME}/src/ironagent"
)

_irondock_filter_warnings() {
  grep -v "^WARN\|^time="
}

_irondock_trim_quotes() {
  local value="$1"
  value="${value#\"}"
  value="${value%\"}"
  printf "%s" "$value"
}

_irondock_mask_value() {
  local value="$1"
  local length=${#value}
  if (( length == 0 )); then
    printf "%s" "<empty>"
    return 0
  fi
  if (( length == 1 )); then
    printf "%s" "<redacted:1 char>"
    return 0
  fi
  printf "%s" "<redacted:${length} chars>"
}

_irondock_read_config_dir() {
  if [[ ! -f "$IRONDOCK_CONFIG" ]]; then
    return 1
  fi
  local raw
  raw=$(sed -n 's/^IRONDOCK_DIR=//p' "$IRONDOCK_CONFIG" | head -n 1)
  if [[ -z "$raw" ]]; then
    return 1
  fi
  _irondock_trim_quotes "$raw"
}

# Ensure IRONDOCK_DIR is set and valid
_irondock_ensure_dir() {
  # Already set and valid?
  if [[ -n "$IRONDOCK_DIR" && -f "${IRONDOCK_DIR}/docker-compose.yml" ]]; then
    return 0
  fi

  # Try loading from config
  local config_dir
  config_dir=$(_irondock_read_config_dir)
  if [[ -n "$config_dir" && -f "${config_dir}/docker-compose.yml" ]]; then
    IRONDOCK_DIR="$config_dir"
    return 0
  fi

  # Auto-detect from common paths
  local found_path=""
  for path in "${IRONDOCK_COMMON_PATHS[@]}"; do
    if [[ -f "${path}/docker-compose.yml" ]]; then
      found_path="$path"
      break
    fi
  done

  if [[ -n "$found_path" ]]; then
    echo ""
    echo "🦞 Found IronAgent at: $found_path"
    echo -n "   Use this location? [Y/n] "
    read -r response
    if [[ "$response" =~ ^[Nn] ]]; then
      echo ""
      echo "Set IRONDOCK_DIR manually:"
      echo "  export IRONDOCK_DIR=/path/to/ironagent"
      return 1
    fi
    IRONDOCK_DIR="$found_path"
  else
    echo ""
    echo "❌ IronAgent not found in common locations."
    echo ""
    echo "Clone it first:"
    echo ""
    echo "  git clone <your-ironagent-repo-url>.git ~/ironagent"
    echo "  cd ~/ironagent && ./scripts/docker/setup.sh"
    echo ""
    echo "Or set IRONDOCK_DIR if it's elsewhere:"
    echo ""
    echo "  export IRONDOCK_DIR=/path/to/ironagent"
    echo ""
    return 1
  fi

  # Save to config
  if [[ ! -d "${HOME}/.irondock" ]]; then
    /bin/mkdir -p "${HOME}/.irondock"
  fi
  echo "IRONDOCK_DIR=\"$IRONDOCK_DIR\"" > "$IRONDOCK_CONFIG"
  echo "✅ Saved to $IRONDOCK_CONFIG"
  echo ""
  return 0
}

# Wrapper to run docker compose commands
_irondock_compose() {
  _irondock_ensure_dir || return 1
  local compose_args=(-f "${IRONDOCK_DIR}/docker-compose.yml")
  if [[ -f "${IRONDOCK_DIR}/docker-compose.extra.yml" ]]; then
    compose_args+=(-f "${IRONDOCK_DIR}/docker-compose.extra.yml")
  fi
  command docker compose "${compose_args[@]}" "$@"
}

_irondock_read_env_token() {
  _irondock_ensure_dir || return 1
  if [[ ! -f "${IRONDOCK_DIR}/.env" ]]; then
    return 1
  fi
  local raw
  raw=$(sed -n 's/^IRONAGENT_GATEWAY_TOKEN=//p' "${IRONDOCK_DIR}/.env" | head -n 1)
  if [[ -z "$raw" ]]; then
    return 1
  fi
  _irondock_trim_quotes "$raw"
}

# Basic Operations
irondock-start() {
  _irondock_compose up -d ironagent-gateway
}

irondock-stop() {
  _irondock_compose down
}

irondock-restart() {
  _irondock_compose restart ironagent-gateway
}

irondock-logs() {
  _irondock_compose logs -f ironagent-gateway
}

irondock-status() {
  _irondock_compose ps
}

# Navigation
irondock-cd() {
  _irondock_ensure_dir || return 1
  cd "${IRONDOCK_DIR}"
}

irondock-config() {
  cd ~/.ironagent
}

irondock-show-config() {
  _irondock_ensure_dir >/dev/null 2>&1 || true
  local config_dir="${HOME}/.ironagent"
  echo -e "${_CLR_BOLD}Config directory:${_CLR_RESET} ${_CLR_CYAN}${config_dir}${_CLR_RESET}"
  echo ""

  # Show ironagent.json
  if [[ -f "${config_dir}/ironagent.json" ]]; then
    echo -e "${_CLR_BOLD}${config_dir}/ironagent.json${_CLR_RESET}"
    echo -e "${_CLR_DIM}$(cat "${config_dir}/ironagent.json")${_CLR_RESET}"
  else
    echo -e "${_CLR_YELLOW}No ironagent.json found${_CLR_RESET}"
  fi
  echo ""

  # Show .env (mask secret values)
  if [[ -f "${config_dir}/.env" ]]; then
    echo -e "${_CLR_BOLD}${config_dir}/.env${_CLR_RESET}"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" =~ ^[[:space:]]*# ]] || [[ -z "$line" ]]; then
        echo -e "${_CLR_DIM}${line}${_CLR_RESET}"
      elif [[ "$line" == *=* ]]; then
        local key="${line%%=*}"
        local val="${line#*=}"
        echo -e "${_CLR_CYAN}${key}${_CLR_RESET}=${_CLR_DIM}$(_irondock_mask_value "$val")${_CLR_RESET}"
      else
        echo -e "${_CLR_DIM}${line}${_CLR_RESET}"
      fi
    done < "${config_dir}/.env"
  else
    echo -e "${_CLR_YELLOW}No .env found${_CLR_RESET}"
  fi
  echo ""

  # Show project .env if available
  if [[ -n "$IRONDOCK_DIR" && -f "${IRONDOCK_DIR}/.env" ]]; then
    echo -e "${_CLR_BOLD}${IRONDOCK_DIR}/.env${_CLR_RESET}"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" =~ ^[[:space:]]*# ]] || [[ -z "$line" ]]; then
        echo -e "${_CLR_DIM}${line}${_CLR_RESET}"
      elif [[ "$line" == *=* ]]; then
        local key="${line%%=*}"
        local val="${line#*=}"
        echo -e "${_CLR_CYAN}${key}${_CLR_RESET}=${_CLR_DIM}$(_irondock_mask_value "$val")${_CLR_RESET}"
      else
        echo -e "${_CLR_DIM}${line}${_CLR_RESET}"
      fi
    done < "${IRONDOCK_DIR}/.env"
  fi
  echo ""
}

irondock-workspace() {
  cd ~/.ironagent/workspace
}

# Container Access
irondock-shell() {
  _irondock_compose exec ironagent-gateway \
    bash -c 'echo "alias ironagent=\"./ironagent.mjs\"" > /tmp/.bashrc_ironagent && bash --rcfile /tmp/.bashrc_ironagent'
}

irondock-exec() {
  _irondock_compose exec ironagent-gateway "$@"
}

irondock-cli() {
  _irondock_compose run --rm ironagent-cli "$@"
}

# Maintenance
irondock-update() {
  _irondock_ensure_dir || return 1

  echo "🔄 Updating IronAgent..."

  echo ""
  echo "📥 Pulling latest source..."
  git -C "${IRONDOCK_DIR}" pull || { echo "❌ git pull failed"; return 1; }

  echo ""
  echo "🔨 Rebuilding Docker image (this may take a few minutes)..."
  _irondock_compose build ironagent-gateway || { echo "❌ Build failed"; return 1; }

  echo ""
  echo "♻️  Recreating container with new image..."
  _irondock_compose down 2>&1 | _irondock_filter_warnings
  _irondock_compose up -d ironagent-gateway 2>&1 | _irondock_filter_warnings

  echo ""
  echo "⏳ Waiting for gateway to start..."
  sleep 5

  echo "✅ Update complete!"
  echo -e "   Verify: $(_cmd irondock-cli status)"
}

irondock-rebuild() {
  _irondock_compose build ironagent-gateway
}

irondock-clean() {
  _irondock_compose down -v --remove-orphans
}

# Health check
irondock-health() {
  _irondock_ensure_dir || return 1
  local token
  token=$(_irondock_read_env_token)
  if [[ -z "$token" ]]; then
    echo "❌ Error: Could not find gateway token"
    echo "   Check: ${IRONDOCK_DIR}/.env"
    return 1
  fi
  _irondock_compose exec -e "IRONAGENT_GATEWAY_TOKEN=$token" ironagent-gateway \
    node dist/index.js health
}

# Show gateway token
irondock-token() {
  _irondock_read_env_token
}

# Fix token configuration (run this once after setup)
irondock-fix-token() {
  _irondock_ensure_dir || return 1

  echo "🔧 Configuring gateway token..."
  local token
  token=$(irondock-token)
  if [[ -z "$token" ]]; then
    echo "❌ Error: Could not find gateway token"
    echo "   Check: ${IRONDOCK_DIR}/.env"
    return 1
  fi

  echo "📝 Setting token: ${token:0:20}..."

  _irondock_compose exec -e "TOKEN=$token" ironagent-gateway \
    bash -c './ironagent.mjs config set gateway.remote.token "$TOKEN" && ./ironagent.mjs config set gateway.auth.token "$TOKEN"' 2>&1 | _irondock_filter_warnings

  echo "🔍 Verifying token was saved..."
  local saved_token
  saved_token=$(_irondock_compose exec ironagent-gateway \
    bash -c "./ironagent.mjs config get gateway.remote.token 2>/dev/null" 2>&1 | _irondock_filter_warnings | tr -d '\r\n' | head -c 64)

  if [[ "$saved_token" == "$token" ]]; then
    echo "✅ Token saved correctly!"
  else
    echo "⚠️  Token mismatch detected"
    echo "   Expected: ${token:0:20}..."
    echo "   Got: ${saved_token:0:20}..."
  fi

  echo "🔄 Restarting gateway..."
  _irondock_compose restart ironagent-gateway 2>&1 | _irondock_filter_warnings

  echo "⏳ Waiting for gateway to start..."
  sleep 5

  echo "✅ Configuration complete!"
  echo -e "   Try: $(_cmd irondock-devices)"
}

# Open dashboard in browser
irondock-dashboard() {
  _irondock_ensure_dir || return 1

  echo "🦞 Getting dashboard URL..."
  local output exit_status url
  output=$(_irondock_compose run --rm ironagent-cli dashboard --no-open 2>&1)
  exit_status=$?
  url=$(printf "%s\n" "$output" | _irondock_filter_warnings | grep -o 'http[s]\?://[^[:space:]]*' | head -n 1)
  if [[ $exit_status -ne 0 ]]; then
    echo "❌ Failed to get dashboard URL"
    echo -e "   Try restarting: $(_cmd irondock-restart)"
    return 1
  fi

  if [[ -n "$url" ]]; then
    echo -e "✅ Opening: ${_CLR_CYAN}${url}${_CLR_RESET}"
    open "$url" 2>/dev/null || xdg-open "$url" 2>/dev/null || echo -e "   Please open manually: ${_CLR_CYAN}${url}${_CLR_RESET}"
    echo ""
    echo -e "${_CLR_CYAN}💡 If you see ${_CLR_RED}'pairing required'${_CLR_CYAN} error:${_CLR_RESET}"
    echo -e "   1. Run: $(_cmd irondock-devices)"
    echo "   2. Copy the Request ID from the Pending table"
    echo -e "   3. Run: $(_cmd 'irondock-approve <request-id>')"
  else
    echo "❌ Failed to get dashboard URL"
    echo -e "   Try restarting: $(_cmd irondock-restart)"
  fi
}

# List device pairings
irondock-devices() {
  _irondock_ensure_dir || return 1

  echo "🔍 Checking device pairings..."
  local output exit_status
  output=$(_irondock_compose exec ironagent-gateway node dist/index.js devices list 2>&1)
  exit_status=$?
  printf "%s\n" "$output" | _irondock_filter_warnings
  if [ $exit_status -ne 0 ]; then
    echo ""
    echo -e "${_CLR_CYAN}💡 If you see token errors above:${_CLR_RESET}"
    echo -e "   1. Verify token is set: $(_cmd irondock-token)"
    echo -e "   2. Try fixing the token automatically: $(_cmd irondock-fix-token)"
    echo "   3. If you still see errors, try manual config inside container:"
    echo -e "      $(_cmd irondock-shell)"
    echo -e "      $(_cmd 'ironagent config get gateway.remote.token')"
    return 1
  fi

  echo ""
  echo -e "${_CLR_CYAN}💡 To approve a pairing request:${_CLR_RESET}"
  echo -e "   $(_cmd 'irondock-approve <request-id>')"
}

# Approve device pairing request
irondock-approve() {
  _irondock_ensure_dir || return 1

  if [[ -z "$1" ]]; then
    echo -e "❌ Usage: $(_cmd 'irondock-approve <request-id>')"
    echo ""
    echo -e "${_CLR_CYAN}💡 How to approve a device:${_CLR_RESET}"
    echo -e "   1. Run: $(_cmd irondock-devices)"
    echo "   2. Find the Request ID in the Pending table (long UUID)"
    echo -e "   3. Run: $(_cmd 'irondock-approve <that-request-id>')"
    echo ""
    echo "Example:"
    echo -e "   $(_cmd 'irondock-approve 6f9db1bd-a1cc-4d3f-b643-2c195262464e')"
    return 1
  fi

  echo "✅ Approving device: $1"
  _irondock_compose exec ironagent-gateway \
    node dist/index.js devices approve "$1" 2>&1 | _irondock_filter_warnings

  echo ""
  echo "✅ Device approved! Refresh your browser."
}

# Show all available irondock helper commands
irondock-help() {
  echo -e "\n${_CLR_BOLD}${_CLR_CYAN}🦞 IronDock - Docker Helpers for IronAgent${_CLR_RESET}\n"

  echo -e "${_CLR_BOLD}${_CLR_MAGENTA}⚡ Basic Operations${_CLR_RESET}"
  echo -e "  $(_cmd irondock-start)       ${_CLR_DIM}Start the gateway${_CLR_RESET}"
  echo -e "  $(_cmd irondock-stop)        ${_CLR_DIM}Stop the gateway${_CLR_RESET}"
  echo -e "  $(_cmd irondock-restart)     ${_CLR_DIM}Restart the gateway${_CLR_RESET}"
  echo -e "  $(_cmd irondock-status)      ${_CLR_DIM}Check container status${_CLR_RESET}"
  echo -e "  $(_cmd irondock-logs)        ${_CLR_DIM}View live logs (follows)${_CLR_RESET}"
  echo ""

  echo -e "${_CLR_BOLD}${_CLR_MAGENTA}🐚 Container Access${_CLR_RESET}"
  echo -e "  $(_cmd irondock-shell)       ${_CLR_DIM}Shell into container (ironagent alias ready)${_CLR_RESET}"
  echo -e "  $(_cmd irondock-cli)         ${_CLR_DIM}Run CLI commands (e.g., irondock-cli status)${_CLR_RESET}"
  echo -e "  $(_cmd irondock-exec) ${_CLR_CYAN}<cmd>${_CLR_RESET}  ${_CLR_DIM}Execute command in gateway container${_CLR_RESET}"
  echo ""

  echo -e "${_CLR_BOLD}${_CLR_MAGENTA}🌐 Web UI & Devices${_CLR_RESET}"
  echo -e "  $(_cmd irondock-dashboard)   ${_CLR_DIM}Open web UI in browser ${_CLR_CYAN}(auto-guides you)${_CLR_RESET}"
  echo -e "  $(_cmd irondock-devices)     ${_CLR_DIM}List device pairings ${_CLR_CYAN}(auto-guides you)${_CLR_RESET}"
  echo -e "  $(_cmd irondock-approve) ${_CLR_CYAN}<id>${_CLR_RESET} ${_CLR_DIM}Approve device pairing ${_CLR_CYAN}(with examples)${_CLR_RESET}"
  echo ""

  echo -e "${_CLR_BOLD}${_CLR_MAGENTA}⚙️  Setup & Configuration${_CLR_RESET}"
  echo -e "  $(_cmd irondock-fix-token)   ${_CLR_DIM}Configure gateway token ${_CLR_CYAN}(run once)${_CLR_RESET}"
  echo ""

  echo -e "${_CLR_BOLD}${_CLR_MAGENTA}🔧 Maintenance${_CLR_RESET}"
  echo -e "  $(_cmd irondock-update)      ${_CLR_DIM}Pull, rebuild, and restart ${_CLR_CYAN}(one-command update)${_CLR_RESET}"
  echo -e "  $(_cmd irondock-rebuild)     ${_CLR_DIM}Rebuild Docker image only${_CLR_RESET}"
  echo -e "  $(_cmd irondock-clean)       ${_CLR_RED}⚠️  Remove containers & volumes (nuclear)${_CLR_RESET}"
  echo ""

  echo -e "${_CLR_BOLD}${_CLR_MAGENTA}🛠️  Utilities${_CLR_RESET}"
  echo -e "  $(_cmd irondock-health)      ${_CLR_DIM}Run health check${_CLR_RESET}"
  echo -e "  $(_cmd irondock-token)       ${_CLR_DIM}Show gateway auth token${_CLR_RESET}"
  echo -e "  $(_cmd irondock-cd)          ${_CLR_DIM}Jump to ironagent project directory${_CLR_RESET}"
  echo -e "  $(_cmd irondock-config)      ${_CLR_DIM}Open config directory (~/.ironagent)${_CLR_RESET}"
  echo -e "  $(_cmd irondock-show-config) ${_CLR_DIM}Print config files with redacted values${_CLR_RESET}"
  echo -e "  $(_cmd irondock-workspace)   ${_CLR_DIM}Open workspace directory${_CLR_RESET}"
  echo ""

  echo -e "${_CLR_BOLD}${_CLR_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${_CLR_RESET}"
  echo -e "${_CLR_BOLD}${_CLR_GREEN}🚀 First Time Setup${_CLR_RESET}"
  echo -e "${_CLR_CYAN}  1.${_CLR_RESET} $(_cmd irondock-start)          ${_CLR_DIM}# Start the gateway${_CLR_RESET}"
  echo -e "${_CLR_CYAN}  2.${_CLR_RESET} $(_cmd irondock-fix-token)      ${_CLR_DIM}# Configure token${_CLR_RESET}"
  echo -e "${_CLR_CYAN}  3.${_CLR_RESET} $(_cmd irondock-dashboard)      ${_CLR_DIM}# Open web UI${_CLR_RESET}"
  echo -e "${_CLR_CYAN}  4.${_CLR_RESET} $(_cmd irondock-devices)        ${_CLR_DIM}# If pairing needed${_CLR_RESET}"
  echo -e "${_CLR_CYAN}  5.${_CLR_RESET} $(_cmd irondock-approve) ${_CLR_CYAN}<id>${_CLR_RESET}   ${_CLR_DIM}# Approve pairing${_CLR_RESET}"
  echo ""

  echo -e "${_CLR_BOLD}${_CLR_GREEN}💬 WhatsApp Setup${_CLR_RESET}"
  echo -e "  $(_cmd irondock-shell)"
  echo -e "    ${_CLR_BLUE}>${_CLR_RESET} $(_cmd 'ironagent channels login --channel whatsapp')"
  echo -e "    ${_CLR_BLUE}>${_CLR_RESET} $(_cmd 'ironagent status')"
  echo ""

  echo -e "${_CLR_BOLD}${_CLR_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${_CLR_RESET}"
  echo ""

  echo -e "${_CLR_CYAN}💡 All commands guide you through next steps!${_CLR_RESET}"
  echo -e "${_CLR_BLUE}📚 Docs: ${_CLR_RESET}${_CLR_CYAN}https://docs.ironagent.ai${_CLR_RESET}"
  echo ""
}
