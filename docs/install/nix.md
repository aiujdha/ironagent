---
summary: "Install IronAgent declaratively with Nix"
read_when:
  - You want reproducible, rollback-able installs
  - You're already using Nix/NixOS/Home Manager
  - You want everything pinned and managed declaratively
title: "Nix"
---

# Nix Installation

Install IronAgent declaratively with **[nix-ironagent](https://github.com/aiujdha/ironagent/tree/feat/webui)** -- a batteries-included Home Manager module.

<Info>
The [nix-ironagent](https://github.com/aiujdha/ironagent/tree/feat/webui) repo is the source of truth for Nix installation. This page is a quick overview.
</Info>

## What You Get

- Gateway + macOS app + tools (whisper, spotify, cameras) -- all pinned
- Launchd service that survives reboots
- Plugin system with declarative config
- Instant rollback: `home-manager switch --rollback`

## Quick Start

<Steps>
  <Step title="Install Determinate Nix">
    If Nix is not already installed, follow the [Determinate Nix installer](https://github.com/DeterminateSystems/nix-installer) instructions.
  </Step>
  <Step title="Create a local flake">
    Use the agent-first template from the nix-ironagent repo:
    ```bash
    mkdir -p ~/code/ironagent-local
    # Copy templates/agent-first/flake.nix from the nix-ironagent repo
    ```
  </Step>
  <Step title="Configure secrets">
    Set up your messaging bot token and model provider API key. Plain files at `~/.secrets/` work fine.
  </Step>
  <Step title="Fill in template placeholders and switch">
    ```bash
    home-manager switch
    ```
  </Step>
  <Step title="Verify">
    Confirm the launchd service is running and your bot responds to messages.
  </Step>
</Steps>

See the [nix-ironagent README](https://github.com/aiujdha/ironagent/tree/feat/webui) for full module options and examples.

## Nix Mode Runtime Behavior

When `IRONAGENT_NIX_MODE=1` is set (automatic with nix-ironagent), IronAgent enters a deterministic mode that disables auto-install flows.

You can also set it manually:

```bash
export IRONAGENT_NIX_MODE=1
```

On macOS, the GUI app does not automatically inherit shell environment variables. Enable Nix mode via defaults instead:

```bash
defaults write ai.ironagent.mac ironagent.nixMode -bool true
```

### What changes in Nix mode

- Auto-install and self-mutation flows are disabled
- Missing dependencies surface Nix-specific remediation messages
- UI surfaces a read-only Nix mode banner

### Config and state paths

IronAgent reads JSON5 config from `IRONAGENT_CONFIG_PATH` and stores mutable data in `IRONAGENT_STATE_DIR`. When running under Nix, set these explicitly to Nix-managed locations so runtime state and config stay out of the immutable store.

| Variable               | Default                                 |
| ---------------------- | --------------------------------------- |
| `IRONAGENT_HOME`        | `HOME` / `USERPROFILE` / `os.homedir()` |
| `IRONAGENT_STATE_DIR`   | `~/.ironagent`                           |
| `IRONAGENT_CONFIG_PATH` | `$IRONAGENT_STATE_DIR/ironagent.json`     |

## Related

- [nix-ironagent](https://github.com/aiujdha/ironagent/tree/feat/webui) -- full setup guide
- [Wizard](/start/wizard) -- non-Nix CLI setup
- [Docker](/install/docker) -- containerized setup
