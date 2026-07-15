---
summary: "Compatibility note for the mistakenly documented `ironagent flows` command"
read_when:
  - You encounter ironagent flows in older release notes, issue threads, or search results
  - You want to know what command replaced ironagent flows
title: "flows"
---

# `ironagent flows`

`ironagent flows` is **not** a current IronAgent CLI command.

Some older release notes and docs mistakenly documented a `flows` command surface. The supported operator surface is [`ironagent tasks`](/automation/tasks).

```bash
ironagent tasks list
ironagent tasks show <lookup>
ironagent tasks cancel <lookup>
```

## Use instead

- `ironagent tasks list` — list tracked background tasks
- `ironagent tasks show <lookup>` — inspect one task by task id, run id, or session key
- `ironagent tasks cancel <lookup>` — cancel a running background task
- `ironagent tasks notify <lookup> <policy>` — change task notification behavior
- `ironagent tasks audit` — surface stale or broken task runs

## Why this page exists

This page stays in place so existing links from older changelog entries, issue threads, and search results have a clear correction instead of a dead end.

## Related

- [Background Tasks](/automation/tasks) — detached work ledger
- [CLI reference](/cli/index) — full command tree
