# IronDock <!-- omit in toc -->

Stop typing `docker-compose` commands. Just type `irondock-start`.

Inspired by Simon Willison's [Running IronAgent in Docker](https://til.simonwillison.net/llms/ironagent-docker).

- [Quickstart](#quickstart)
- [Available Commands](#available-commands)
  - [Basic Operations](#basic-operations)
  - [Container Access](#container-access)
  - [Web UI \& Devices](#web-ui--devices)
  - [Setup \& Configuration](#setup--configuration)
  - [Maintenance](#maintenance)
  - [Utilities](#utilities)
- [Configuration \& Secrets](#configuration--secrets)
  - [Docker Files](#docker-files)
  - [Config Files](#config-files)
  - [Initial Setup](#initial-setup)
  - [How It Works in Docker](#how-it-works-in-docker)
  - [Env Precedence](#env-precedence)
- [Common Workflows](#common-workflows)
  - [Check Status and Logs](#check-status-and-logs)
  - [Set Up WhatsApp Bot](#set-up-whatsapp-bot)
  - [Troubleshooting Device Pairing](#troubleshooting-device-pairing)
  - [Fix Token Mismatch Issues](#fix-token-mismatch-issues)
  - [Permission Denied](#permission-denied)
- [Requirements](#requirements)
- [Development](#development)

## Quickstart

**Install:**

```bash
mkdir -p ~/.irondock && curl -sL https://raw.githubusercontent.com/ironagent/ironagent/main/scripts/irondock/irondock-helpers.sh -o ~/.irondock/irondock-helpers.sh
```

```bash
echo 'source ~/.irondock/irondock-helpers.sh' >> ~/.zshrc && source ~/.zshrc
```

Canonical docs page: https://docs.ironagent.ai/install/irondock

If you previously installed IronDock from `scripts/shell-helpers/irondock-helpers.sh`, rerun the install command above. The old raw GitHub path has been removed.

**See what you get:**

```bash
irondock-help
```

On first command, IronDock auto-detects your IronAgent directory:

- Checks common paths (`~/ironagent`, `~/workspace/ironagent`, etc.)
- If found, asks you to confirm
- Saves to `~/.irondock/config`

**First time setup:**

```bash
irondock-start
```

```bash
irondock-fix-token
```

```bash
irondock-dashboard
```

If you see "pairing required":

```bash
irondock-devices
```

And approve the request for the specific device:

```bash
irondock-approve <request-id>
```

## Available Commands

### Basic Operations

| Command            | Description                     |
| ------------------ | ------------------------------- |
| `irondock-start`   | Start the gateway               |
| `irondock-stop`    | Stop the gateway                |
| `irondock-restart` | Restart the gateway             |
| `irondock-status`  | Check container status          |
| `irondock-logs`    | View live logs (follows output) |

### Container Access

| Command                   | Description                                    |
| ------------------------- | ---------------------------------------------- |
| `irondock-shell`          | Interactive shell inside the gateway container |
| `irondock-cli <command>`  | Run IronAgent CLI commands                     |
| `irondock-exec <command>` | Execute arbitrary commands in the container    |

### Web UI & Devices

| Command                 | Description                                |
| ----------------------- | ------------------------------------------ |
| `irondock-dashboard`    | Open web UI in browser with authentication |
| `irondock-devices`      | List device pairing requests               |
| `irondock-approve <id>` | Approve a device pairing request           |

### Setup & Configuration

| Command              | Description                                       |
| -------------------- | ------------------------------------------------- |
| `irondock-fix-token` | Configure gateway authentication token (run once) |

### Maintenance

| Command            | Description                                           |
| ------------------ | ----------------------------------------------------- |
| `irondock-update`  | Pull latest, rebuild image, and restart (one command) |
| `irondock-rebuild` | Rebuild the Docker image only                         |
| `irondock-clean`   | Remove all containers and volumes (destructive!)      |

### Utilities

| Command                | Description                               |
| ---------------------- | ----------------------------------------- |
| `irondock-health`      | Run gateway health check                  |
| `irondock-token`       | Display the gateway authentication token  |
| `irondock-cd`          | Jump to the IronAgent project directory   |
| `irondock-config`      | Open the IronAgent config directory       |
| `irondock-show-config` | Print config files with redacted values   |
| `irondock-workspace`   | Open the workspace directory              |
| `irondock-help`        | Show all available commands with examples |

## Configuration & Secrets

The Docker setup uses three config files on the host. The container never stores secrets — everything is bind-mounted from local files.

### Docker Files

| File                       | Purpose                                                                      |
| -------------------------- | ---------------------------------------------------------------------------- |
| `Dockerfile`               | Builds the `ironagent:local` image (Node 22, pnpm, non-root `node` user)     |
| `docker-compose.yml`       | Defines `ironagent-gateway` and `ironagent-cli` services, bind-mounts, ports |
| `docker-setup.sh`          | First-time setup — builds image, creates `.env` from `.env.example`          |
| `.env.example`             | Template for `<project>/.env` with all supported vars and docs               |
| `docker-compose.extra.yml` | Optional overrides — auto-loaded by IronDock helpers if present              |

### Config Files

| File                          | Purpose                                          | Examples                                                               |
| ----------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------- |
| `<project>/.env`              | **Docker infra** — image, ports, gateway token   | `IRONAGENT_GATEWAY_TOKEN`, `IRONAGENT_IMAGE`, `IRONAGENT_GATEWAY_PORT` |
| `~/.ironagent/.env`           | **Secrets** — API keys and bot tokens            | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`            |
| `~/.ironagent/ironagent.json` | **Behavior config** — models, channels, policies | Model selection, WhatsApp allowlists, agent settings                   |

**Do NOT** put API keys or bot tokens in `ironagent.json`. Use `~/.ironagent/.env` for all secrets.

### Initial Setup

`./docker-setup.sh` (in the project root) handles first-time Docker configuration:

- Builds the `ironagent:local` image from `Dockerfile`
- Creates `<project>/.env` from `.env.example` with a generated gateway token
- Sets up `~/.ironagent` directories if they don't exist

```bash
./docker-setup.sh
```

After setup, add your API keys:

```bash
vim ~/.ironagent/.env
```

See `.env.example` for all supported keys.

The `Dockerfile` supports two optional build args:

- `IRONAGENT_DOCKER_APT_PACKAGES` — extra apt packages to install (e.g. `ffmpeg`)
- `IRONAGENT_INSTALL_BROWSER=1` — pre-install Chromium for browser automation (adds ~300MB, but skips the 60-90s Playwright install on each container start)

### How It Works in Docker

`docker-compose.yml` bind-mounts both config and workspace from the host:

```yaml
volumes:
  - ${IRONAGENT_CONFIG_DIR}:/home/node/.ironagent
  - ${IRONAGENT_WORKSPACE_DIR}:/home/node/.ironagent/workspace
```

This means:

- `~/.ironagent/.env` is available inside the container at `/home/node/.ironagent/.env` — IronAgent loads it automatically as the global env fallback
- `~/.ironagent/ironagent.json` is available at `/home/node/.ironagent/ironagent.json` — the gateway watches it and hot-reloads most changes
- No need to add API keys to `docker-compose.yml` or configure anything inside the container
- Keys survive `irondock-update`, `irondock-rebuild`, and `irondock-clean` because they live on the host

The project `.env` feeds Docker Compose directly (gateway token, image name, ports). The `~/.ironagent/.env` feeds the IronAgent process inside the container.

### Example `~/.ironagent/.env`

```bash
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABCDEF...
```

### Example `<project>/.env`

```bash
IRONAGENT_CONFIG_DIR=/Users/you/.ironagent
IRONAGENT_WORKSPACE_DIR=/Users/you/.ironagent/workspace
IRONAGENT_GATEWAY_PORT=18789
IRONAGENT_BRIDGE_PORT=18790
IRONAGENT_GATEWAY_BIND=lan
IRONAGENT_GATEWAY_TOKEN=<generated-by-docker-setup>
IRONAGENT_IMAGE=ironagent:local
```

### Env Precedence

IronAgent loads env vars in this order (highest wins, never overrides existing):

1. **Process environment** — `docker-compose.yml` `environment:` block (gateway token, session keys)
2. **`.env` in CWD** — project root `.env` (Docker infra vars)
3. **`~/.ironagent/.env`** — global secrets (API keys, bot tokens)
4. **`ironagent.json` `env` block** — inline vars, applied only if still missing
5. **Shell env import** — optional login-shell scrape (`IRONAGENT_LOAD_SHELL_ENV=1`)

## Common Workflows

### Update IronAgent

> **Important:** `ironagent update` does not work inside Docker.
> The container runs as a non-root user with a source-built image, so `npm i -g` fails with EACCES.
> Use `irondock-update` instead — it pulls, rebuilds, and restarts from the host.

```bash
irondock-update
```

This runs `git pull` → `docker compose build` → `docker compose down/up` in one step.

If you only want to rebuild without pulling:

```bash
irondock-rebuild && irondock-stop && irondock-start
```

### Check Status and Logs

**Restart the gateway:**

```bash
irondock-restart
```

**Check container status:**

```bash
irondock-status
```

**View live logs:**

```bash
irondock-logs
```

### Set Up WhatsApp Bot

**Shell into the container:**

```bash
irondock-shell
```

**Inside the container, login to WhatsApp:**

```bash
ironagent channels login --channel whatsapp --verbose
```

Scan the QR code with WhatsApp on your phone.

**Verify connection:**

```bash
ironagent status
```

### Troubleshooting Device Pairing

**Check for pending pairing requests:**

```bash
irondock-devices
```

**Copy the Request ID from the "Pending" table, then approve:**

```bash
irondock-approve <request-id>
```

Then refresh your browser.

### Fix Token Mismatch Issues

If you see "gateway token mismatch" errors:

```bash
irondock-fix-token
```

This will:

1. Read the token from your `.env` file
2. Configure it in the IronAgent config
3. Restart the gateway
4. Verify the configuration

### Permission Denied

**Ensure Docker is running and you have permission:**

```bash
docker ps
```

## Requirements

- Docker and Docker Compose installed
- Bash or Zsh shell
- IronAgent project (run `scripts/docker/setup.sh`)

## Development

**Test with fresh config (mimics first-time install):**

```bash
unset IRONDOCK_DIR && rm -f ~/.irondock/config && source scripts/irondock/irondock-helpers.sh
```

Then run any command to trigger auto-detect:

```bash
irondock-start
```
