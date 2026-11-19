# Security policy

AiVa enforces security and data-protection controls, so we take reports about it seriously.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
open the repository's **Security** tab and choose **Report a vulnerability**.

Include what you can of:

- the component and version (commit) affected;
- how to reproduce it, ideally with a minimal request or configuration;
- what an attacker could achieve, for example bypassing a policy decision, reaching an external model with
  confidential data, reading another caller's runs, or exhausting a budget.

We aim to acknowledge a report within five working days and to agree a disclosure date with you once a fix is
ready. Please give us reasonable time to fix the issue before disclosing it.

## Scope

In scope: the gateway (`gateway/`), the policy (`policies/`), the Control Center (`web/`) and the container images
built from `deploy/`. Weaknesses in the [design guarantees](README.md#design-guarantees) are the most important
reports we can receive.

Out of scope: findings that need an already-compromised host or administrator credentials, denial of service by
volume alone, and the synthetic sample data in `config/` and `data/`, which contains no real people or secrets.

## Supported versions

Security fixes are made on the `main` branch.
