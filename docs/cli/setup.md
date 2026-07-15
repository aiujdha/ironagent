---
summary: "CLI reference for `ironagent setup` (initialize config + workspace)"
read_when:
  - You’re doing first-run setup without full CLI onboarding
  - You want to set the default workspace path
title: "setup"
---

# `ironagent setup`

Initialize `~/.ironagent/ironagent.json` and the agent workspace.

Related:

- Getting started: [Getting started](/start/getting-started)
- CLI onboarding: [Onboarding (CLI)](/start/wizard)

## Examples

```bash
ironagent setup
ironagent setup --workspace ~/.ironagent/workspace
```

To run onboarding via setup:

```bash
ironagent setup --wizard
```
