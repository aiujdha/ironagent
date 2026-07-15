# Summary

- TBD

# Why

- Linked issue / context:
- User or operator impact:

# Scope

- TBD

# Tests

- [ ] `git diff --check`
- [ ] Go touched: `cd go-gateway && go test -count=1 ./... && go vet ./...`
- [ ] Python touched: run the relevant existing Python checks
- [ ] Deployment touched: `kubectl kustomize <overlay>`
- [ ] Docs only: links and commands reviewed

Evidence or reason a check was not run:

# Risk And Rollback

- Risk:
- Rollback:

# Review Notes

- Security boundary affected: none / describe
- Compatibility or migration impact: none / describe

# Checklist

- [ ] This PR changes only the minimum necessary files.
- [ ] No unrelated refactor, formatting, comment cleanup, or rename is included.
- [ ] No secrets, tokens, passwords, private URLs, or personal machine paths are committed.
- [ ] I have read `docs/START_HERE.md`.
- [ ] I have followed `docs/AI_CODING_RULES.md`.
- [ ] Interface changes are reflected in `docs/接口契约.md`, or this PR does not change interfaces.
- [ ] Event changes are reflected in `docs/事件契约.md`, or this PR does not change events.
- [ ] State or stage changes are reflected in `docs/状态机.md`, or this PR does not change states/stages.
- [ ] System boundary changes are reflected in `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`, or this PR does not change boundaries.
- [ ] Local startup changes are reflected in `docs/LOCAL_STARTUP.md`, or this PR does not change startup.
- [ ] All review comments are either resolved or explicitly tracked.
