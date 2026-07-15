# Security Policy

IronAgent is a security-sensitive control plane. Please do not report vulnerabilities, leaked credentials, authentication bypasses, or deployment secrets in public Issues or Discussions.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** form in the repository Security tab. Include:

- the affected component and revision;
- prerequisites and a minimal reproduction;
- expected and observed behavior;
- potential impact;
- suggested remediation, if known.

Do not include real customer data or production credentials. Use synthetic examples and redact logs before attaching them.

If private vulnerability reporting is not enabled for the repository, contact a maintainer through a private channel and ask for a secure reporting path. Do not open a public issue containing vulnerability details.

## Response expectations

Maintainers will acknowledge a complete report, assess severity, and coordinate disclosure after a fix or mitigation is available. Timelines depend on impact and reproducibility; no public disclosure date should be assumed until it is agreed with the maintainers.

## Supported versions

Until the project publishes versioned releases, security fixes are applied to the default branch only. Deployment operators should track the latest reviewed release or commit and rotate any credential that may have been exposed.
