---
summary: "Uninstall IronAgent completely (CLI, service, state, workspace)"
read_when:
  - You want to remove IronAgent from a machine
  - The gateway service is still running after uninstall
title: "Uninstall"
---

# Uninstall

Two paths:

- **Easy path** if `ironagent` is still installed.
- **Manual service removal** if the CLI is gone but the service is still running.

## Easy path (CLI still installed)

Recommended: use the built-in uninstaller:

```bash
ironagent uninstall
```

Non-interactive (automation / npx):

```bash
ironagent uninstall --all --yes --non-interactive
npx -y ironagent uninstall --all --yes --non-interactive
```

Manual steps (same result):

1. Stop the gateway service:

```bash
ironagent gateway stop
```

2. Uninstall the gateway service (launchd/systemd/schtasks):

```bash
ironagent gateway uninstall
```

3. Delete state + config:

```bash
rm -rf "${IRONAGENT_STATE_DIR:-$HOME/.ironagent}"
```

If you set `IRONAGENT_CONFIG_PATH` to a custom location outside the state dir, delete that file too.

4. Delete your workspace (optional, removes agent files):

```bash
rm -rf ~/.ironagent/workspace
```

5. Remove the CLI install (pick the one you used):

```bash
npm rm -g ironagent
pnpm remove -g ironagent
bun remove -g ironagent
```

6. If you installed the macOS app:

```bash
rm -rf /Applications/IronAgent.app
```

Notes:

- If you used profiles (`--profile` / `IRONAGENT_PROFILE`), repeat step 3 for each state dir (defaults are `~/.ironagent-<profile>`).
- In remote mode, the state dir lives on the **gateway host**, so run steps 1-4 there too.

## Manual service removal (CLI not installed)

Use this if the gateway service keeps running but `ironagent` is missing.

### macOS (launchd)

Default label is `ai.ironagent.gateway` (or `ai.ironagent.<profile>`; legacy `com.ironagent.*` may still exist):

```bash
launchctl bootout gui/$UID/ai.ironagent.gateway
rm -f ~/Library/LaunchAgents/ai.ironagent.gateway.plist
```

If you used a profile, replace the label and plist name with `ai.ironagent.<profile>`. Remove any legacy `com.ironagent.*` plists if present.

### Linux (systemd user unit)

Default unit name is `ironagent-gateway.service` (or `ironagent-gateway-<profile>.service`):

```bash
systemctl --user disable --now ironagent-gateway.service
rm -f ~/.config/systemd/user/ironagent-gateway.service
systemctl --user daemon-reload
```

### Windows (Scheduled Task)

Default task name is `IronAgent Gateway` (or `IronAgent Gateway (<profile>)`).
The task script lives under your state dir.

```powershell
schtasks /Delete /F /TN "IronAgent Gateway"
Remove-Item -Force "$env:USERPROFILE\.ironagent\gateway.cmd"
```

If you used a profile, delete the matching task name and `~\.ironagent-<profile>\gateway.cmd`.

## Normal install vs source checkout

### Normal install (install.sh / npm / pnpm / bun)

If you used `https://ironagent.ai/install.sh` or `install.ps1`, the CLI was installed with `npm install -g ironagent@latest`.
Remove it with `npm rm -g ironagent` (or `pnpm remove -g` / `bun remove -g` if you installed that way).

### Source checkout (git clone)

If you run from a repo checkout (`git clone` + `ironagent ...` / `bun run ironagent ...`):

1. Uninstall the gateway service **before** deleting the repo (use the easy path above or manual service removal).
2. Delete the repo directory.
3. Remove state + workspace as shown above.
