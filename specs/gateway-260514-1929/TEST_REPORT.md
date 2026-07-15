# gateway-260514-1929 TEST_REPORT

- Date: 2026-05-18
- Branch: `codex/admin-260514-2331-reaccept-final`
- Round start HEAD: `591171300066180acca0fe9f1474975afcfcc962`
- Lane: Lane 1 Gateway control session security closure
- Status: partial/code-present

## Result

This pass closes a narrow code-level gap in the control-session approval flow.
It does not verify the full gateway spec in a browser, reverse proxy, NodePort,
or deployed environment.

Code-level evidence now covers:

- Control UI launch ticket creation no longer requests browser-visible
  internal service credentials by default.
- Ticket resolve payload no longer includes internal service credentials when the default
  request path is used.
- Browser control-session approval resolve is available at:
  `/assistants/{assistant_id}/control/api/v1/approvals/{approval_id}/resolve`
- Approval resolve requires the control-session assistant scope to match the
  approval assistant scope.
- Already-resolved approvals return conflict instead of being replayed.
- Gateway approval responses include `assistant_id` so the router can enforce
  scope.

## Tests Run

- `/opt/homebrew/Cellar/go/1.25.6/libexec/bin/go test ./internal/httpapi`
  - Result: passed in the Lane 1 worker.
- `.venv/bin/python -m pytest -q tests/integration/test_control_ui_launch_ticket.py`
  - Result: passed in the Lane 1 worker.

## Evidence Mapping

| Behavior | Status | Evidence |
| --- | --- | --- |
| B4 browser control API does not rely on frontend admin/runtime token for approval resolve | code-present | Go control-session approval resolve route and Python launch-ticket tests |
| B7 approval resolve uses control session and assistant scope | code-present | `TestAssistantControlApprovalResolveUsesSessionAssistantScope` |
| B10 real internal service credential is not returned to browser by default | code-present | `test_control_ui_launch_ticket_minter_uses_internal_service_auth`; ticket consume test asserts the exact response keys |

## Not Closed

The following behaviors remain unverified in this round:

- B1: No current browser URL/HTML/storage evidence proving the opened control
  page contains no secrets.
- B2/B3: No complete ticket resolve acceptance matrix for expiry/reuse/origin
  failures in this round.
- B5: No live UI evidence for session expiration or Gateway restart behavior.
- B6: Workspace-files control-session coverage was not reverified in this
  round.
- B8/B9: Multi-approval UI behavior and admin/system approval separation were
  not verified against a target UI tree.
- B11: Cookie attributes were not verified under HTTP, HTTPS reverse proxy, or
  NodePort deployment.
- B12: Rollback behavior was not exercised.

## Notes

`PRODUCT.md` and `TECH.md` stay `ready`. This report records only selected
code-level evidence for B4/B7/B10 and must not be used as full gateway
verification.

2026-05-18 added `specs/runtime-260518-1025/` for runtime registry
multi-node consistency. This report does not verify that new spec.
