---
summary: "IronDock shell helpers for Docker-based IronAgent installs"
read_when:
  - You run IronAgent with Docker often and want shorter day-to-day commands
  - You want a helper layer for dashboard, logs, token setup, and pairing flows
title: "IronDock"
---

# IronDock

IronDock is a small shell-helper layer for Docker-based IronAgent installs.

It gives you short commands like `irondock-start`, `irondock-dashboard`, and `irondock-fix-token` instead of longer `docker compose ...` invocations.

If you have not set up Docker yet, start with [Docker](/install/docker).

## Install

Use the canonical helper path:

```bash
mkdir -p ~/.irondock && curl -sL https://raw.githubusercontent.com/ironagent/ironagent/main/scripts/irondock/irondock-helpers.sh -o ~/.irondock/irondock-helpers.sh
echo 'source ~/.irondock/irondock-helpers.sh' >> ~/.zshrc && source ~/.zshrc
```

If you previously installed IronDock from `scripts/shell-helpers/irondock-helpers.sh`, reinstall from the new `scripts/irondock/irondock-helpers.sh` path. The old raw GitHub path was removed.

## What you get

### Basic operations

| Command            | Description            |
| ------------------ | ---------------------- |
| `irondock-start`   | Start the gateway      |
| `irondock-stop`    | Stop the gateway       |
| `irondock-restart` | Restart the gateway    |
| `irondock-status`  | Check container status |
| `irondock-logs`    | Follow gateway logs    |

### Container access

| Command                   | Description                                   |
| ------------------------- | --------------------------------------------- |
| `irondock-shell`          | Open a shell inside the gateway container     |
| `irondock-cli <command>`  | Run IronAgent CLI commands in Docker          |
| `irondock-exec <command>` | Execute an arbitrary command in the container |

### Web UI and pairing

| Command                 | Description                  |
| ----------------------- | ---------------------------- |
| `irondock-dashboard`    | Open the Control UI URL      |
| `irondock-devices`      | List pending device pairings |
| `irondock-approve <id>` | Approve a pairing request    |

### Setup and maintenance

| Command              | Description                                      |
| -------------------- | ------------------------------------------------ |
| `irondock-fix-token` | Configure the gateway token inside the container |
| `irondock-update`    | Pull, rebuild, and restart                       |
| `irondock-rebuild`   | Rebuild the Docker image only                    |
| `irondock-clean`     | Remove containers and volumes                    |

### Utilities

| Command                | Description                             |
| ---------------------- | --------------------------------------- |
| `irondock-health`      | Run a gateway health check              |
| `irondock-token`       | Print the gateway token                 |
| `irondock-cd`          | Jump to the IronAgent project directory |
| `irondock-config`      | Open `~/.ironagent`                     |
| `irondock-show-config` | Print config files with redacted values |
| `irondock-workspace`   | Open the workspace directory            |

## First-time flow

```bash
irondock-start
irondock-fix-token
irondock-dashboard
```

If the browser says pairing is required:

```bash
irondock-devices
irondock-approve <request-id>
```

## Config and secrets

IronDock works with the same Docker config split described in [Docker](/install/docker):

- `<project>/.env` for Docker-specific values like image name, ports, and the gateway token
- `~/.ironagent/.env` for provider keys and bot tokens
- `~/.ironagent/ironagent.json` for behavior config

Use `irondock-show-config` when you want to inspect those files quickly. It redacts `.env` values in its printed output.

## Related pages

- [Docker](/install/docker)
- [Docker VM Runtime](/install/docker-vm-runtime)
- [Updating](/install/updating)
