# Open Source Readiness

This document records repository-specific release gates. It is not a substitute for legal review or production credential rotation.

## Blocking gates

- [x] The copyright owner selected and added the Apache License 2.0.
- [ ] Rotate every credential that existed in the private source repository before this snapshot.
- [x] Publish from a fresh root commit instead of exposing the private repository history.
- [x] Publish only the GitHub account's noreply identity in the new Git history.
- [x] Exclude internal `output/`, archived evidence, spreadsheets, and `ironagent-main.bundle`.
- [ ] Enable GitHub private vulnerability reporting.

## Public snapshot (2026-07-15)

- The snapshot contains 596 tracked source and documentation files.
- The Git history contains one root commit.
- Redacted Gitleaks directory and full-history scans report no leaks.
- Active deployment examples use placeholder credentials and documentation-only IP ranges/domains.
- Internal evidence directories and binary test artifacts are not part of the snapshot.

## Repository quality gates

- [x] Contributor guidance documents architecture-aware validation.
- [x] A security reporting policy avoids public vulnerability disclosure.
- [x] Pull requests have a scope, evidence, risk, and rollback template.
- [x] GitHub Actions run Python tests, lint changed Python files, validate Go, and scan Git history for secrets.
- [ ] Resolve the existing full-repository Ruff backlog before making full-tree lint a required check.
- [ ] Configure branch protection using `.github/BRANCH_PROTECTION.md`.
- [ ] Add `CODEOWNERS` after GitHub teams or additional maintainers are known.

## Publication sequence

1. Rotate leaked credentials in every affected external system.
2. Push this fresh-root snapshot to a private GitHub repository.
3. Verify Actions, branch protection, private vulnerability reporting, and default-branch settings.
4. Review repository metadata, topics, description, and public documentation.
5. Make the GitHub repository public only after all blocking gates are complete.

Never push or mirror the private GitLab history to the public repository.
