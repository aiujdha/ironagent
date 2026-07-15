---
summary: "Updating IronAgent safely (global install or source), plus rollback strategy"
read_when:
  - Updating IronAgent
  - Something breaks after an update
title: "Updating"
---

# Updating

Keep IronAgent up to date.

## Recommended: `ironagent update`

The fastest way to update. It detects your install type (npm or git), fetches the latest version, runs `ironagent doctor`, and restarts the gateway.

```bash
ironagent update
```

To switch channels or target a specific version:

```bash
ironagent update --channel beta
ironagent update --tag main
ironagent update --dry-run   # preview without applying
```

See [Development channels](/install/development-channels) for channel semantics.

## Alternative: re-run the installer

```bash
curl -fsSL https://ironagent.ai/install.sh | bash
```

Add `--no-onboard` to skip onboarding. For source installs, pass `--install-method git --no-onboard`.

## Alternative: manual npm or pnpm

```bash
npm i -g ironagent@latest
```

```bash
pnpm add -g ironagent@latest
```

## Auto-updater

The auto-updater is off by default. Enable it in `~/.ironagent/ironagent.json`:

```json5
{
  update: {
    channel: "stable",
    auto: {
      enabled: true,
      stableDelayHours: 6,
      stableJitterHours: 12,
      betaCheckIntervalHours: 1,
    },
  },
}
```

| Channel  | Behavior                                                                                                      |
| -------- | ------------------------------------------------------------------------------------------------------------- |
| `stable` | Waits `stableDelayHours`, then applies with deterministic jitter across `stableJitterHours` (spread rollout). |
| `beta`   | Checks every `betaCheckIntervalHours` (default: hourly) and applies immediately.                              |
| `dev`    | No automatic apply. Use `ironagent update` manually.                                                           |

The gateway also logs an update hint on startup (disable with `update.checkOnStart: false`).

## After updating

<Steps>

### Run doctor

```bash
ironagent doctor
```

Migrates config, audits DM policies, and checks gateway health. Details: [Doctor](/gateway/doctor)

### Restart the gateway

```bash
ironagent gateway restart
```

### Verify

```bash
ironagent health
```

</Steps>

## Rollback

### Pin a version (npm)

```bash
npm i -g ironagent@<version>
ironagent doctor
ironagent gateway restart
```

Tip: `npm view ironagent version` shows the current published version.

### Pin a commit (source)

```bash
git fetch origin
git checkout "$(git rev-list -n 1 --before=\"2026-01-01\" origin/main)"
pnpm install && pnpm build
ironagent gateway restart
```

To return to latest: `git checkout main && git pull`.

## If you are stuck

- Run `ironagent doctor` again and read the output carefully.
- Check: [Troubleshooting](/gateway/troubleshooting)
- Ask in Discord: [https://discord.gg/irond](https://discord.gg/irond)

## Related

- [Install Overview](/install) — all installation methods
- [Doctor](/gateway/doctor) — health checks after updates
- [Migrating](/install/migrating) — major version migration guides
