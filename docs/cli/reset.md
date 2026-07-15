---
summary: "CLI reference for `ironagent reset` (reset local state/config)"
read_when:
  - You want to wipe local state while keeping the CLI installed
  - You want a dry-run of what would be removed
title: "reset"
---

# `ironagent reset`

Reset local config/state (keeps the CLI installed).

```bash
ironagent backup create
ironagent reset
ironagent reset --dry-run
ironagent reset --scope config+creds+sessions --yes --non-interactive
```

Run `ironagent backup create` first if you want a restorable snapshot before removing local state.
