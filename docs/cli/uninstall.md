---
summary: "CLI reference for `ironagent uninstall` (remove gateway service + local data)"
read_when:
  - You want to remove the gateway service and/or local state
  - You want a dry-run first
title: "uninstall"
---

# `ironagent uninstall`

Uninstall the gateway service + local data (CLI remains).

```bash
ironagent backup create
ironagent uninstall
ironagent uninstall --all --yes
ironagent uninstall --dry-run
```

Run `ironagent backup create` first if you want a restorable snapshot before removing state or workspaces.
