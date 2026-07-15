---
summary: "Compatibility note for older IronFlow references in release notes and docs"
read_when:
  - You encounter IronFlow or ironagent flows in older release notes or docs
  - You want to understand what IronFlow terminology maps to in the current CLI
  - You want to translate older flow references into the supported task commands
title: "IronFlow"
---

# IronFlow

`IronFlow` appears in some older IronAgent release notes and documentation as if it were a user-facing runtime with its own `ironagent flows` command surface.

That is not the current operator-facing surface in this repository.

Today, the supported CLI surface for inspecting and managing detached work is [`ironagent tasks`](/automation/tasks).

## What to use today

- `ironagent tasks list` shows tracked detached runs
- `ironagent tasks show <lookup>` shows one task by task id, run id, or session key
- `ironagent tasks cancel <lookup>` cancels a running task
- `ironagent tasks audit` surfaces stale or broken task runs

```bash
ironagent tasks list
ironagent tasks show <lookup>
ironagent tasks cancel <lookup>
```

## What this means for older references

If you see `IronFlow` or `ironagent flows` in:

- old release notes
- issue threads
- stale search results
- outdated local notes

translate those instructions to the current task CLI:

- `ironagent flows list` -> `ironagent tasks list`
- `ironagent flows show <lookup>` -> `ironagent tasks show <lookup>`
- `ironagent flows cancel <lookup>` -> `ironagent tasks cancel <lookup>`

## Related

- [Background Tasks](/automation/tasks) — detached work ledger
- [CLI: flows](/cli/flows) — compatibility note for the mistaken command name
- [Cron Jobs](/automation/cron-jobs) — scheduled jobs that may create tasks
