# runtime-260513-1949 TEST_REPORT

- Date: 2026-05-18
- Branch: `codex/admin-260514-2331-reaccept-final`
- Lane: Lane 2 Runtime capability evidence
- Status: partial/code-present

## Result

This pass is partial/code-present only. It is not verified against a live admin UI,
running assistant container, real policy edit flow, or runtime tool invocation.

Code-level evidence now covers the narrow runtime workspace/tool-surface items:

- Default `iron_agent_seed_workspace_skills` is empty, so new workspaces no longer
  seed default skills unless explicitly configured.
- Runtime container managed env includes:
  - `IRON_AGENT_WORKSPACE_SKILLS_ONLY=1`
  - `IRON_AGENT_WORKSPACE_PLUGINS_ONLY=1`
- Workspace item discovery recognizes plugin packages whose metadata file is
  `ironagent.plugin.json`.

## Tests Run

- `.venv/bin/python -m pytest tests/unit/test_runtime_provisioner.py tests/unit/test_assistant_service.py`
  - Result: passed, 71 tests
- `.venv/bin/python -m py_compile src/runtime/provisioner.py src/shared/config.py src/assistants/service.py tests/unit/test_runtime_provisioner.py tests/unit/test_assistant_service.py`
  - Result: passed

## Evidence Mapping

| Item | Status | Evidence |
| --- | --- | --- |
| Default workspace skills are not seeded | code-present | `test_provisioner_does_not_seed_workspace_skills_by_default` |
| Runtime container limits skills/plugins to workspace sources | code-present | container env assertions in runtime provisioner tests, including K8s env |
| `ironagent.plugin.json` is recognized as workspace item content | code-present | `test_assistant_service_lists_workspace_items_with_disabled_entries` |

## Not Closed

The following acceptance behaviors remain open and must not be marked verified:

- B1: No live admin/API evidence that policy edits clearly show restart-required
  or next-start effect scope.
- B3: No live evidence that a running assistant is not shown as already updated
  when hot update is unsupported.
- B4: No live evidence that affected assistants are identified after a policy
  change.
- B6: No active-task simulation proving restart waits for task completion.
- B10: No audit evidence for policy edit plus apply/restart actions.

## Notes

This report intentionally does not claim B1/B3/B4/B6/B10 closure. The current
change only establishes code-level runtime workspace/tool-surface controls and
unit coverage for those controls.

2026-05-18 added `specs/runtime-260518-1025/` for the broader runtime-state
consistency model. This report does not verify that new spec.
