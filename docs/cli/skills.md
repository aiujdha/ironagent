---
summary: "CLI reference for `ironagent skills` (search/install/update/list/info/check)"
read_when:
  - You want to see which skills are available and ready to run
  - You want to search, install, or update skills from IronHub
  - You want to debug missing binaries/env/config for skills
title: "skills"
---

# `ironagent skills`

Inspect local skills and install/update skills from IronHub.

Related:

- Skills system: [Skills](/tools/skills)
- Skills config: [Skills config](/tools/skills-config)
- IronHub installs: [IronHub](/tools/ironhub)

## Commands

```bash
ironagent skills search "calendar"
ironagent skills install <slug>
ironagent skills install <slug> --version <version>
ironagent skills update <slug>
ironagent skills update --all
ironagent skills list
ironagent skills list --eligible
ironagent skills info <name>
ironagent skills check
```

`search`/`install`/`update` use IronHub directly and install into the active
workspace `skills/` directory. `list`/`info`/`check` still inspect the local
skills visible to the current workspace and config.

This CLI `install` command downloads skill folders from IronHub. Gateway-backed
skill dependency installs triggered from onboarding or Skills settings use the
separate `skills.install` request path instead.
