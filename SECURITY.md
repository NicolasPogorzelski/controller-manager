# Security Policy

## Scope

Controller Manager installs a root-owned helper (`controller-hidraw-gate`) and a scoped
NOPASSWD sudoers rule. The helper only ever `chmod`s `/dev/hidraw*` nodes belonging to
controllers on a built-in vendor allowlist. If you find a way to bypass these constraints
or to escalate privileges through any part of this project, please report it privately.

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Report privately via GitHub: go to the
[Security tab](https://github.com/NicolasPogorzelski/controller-manager/security/advisories)
and choose **Report a vulnerability**. You will receive a response within 7 days.

Please include:
- A description of the vulnerability and its impact
- Steps to reproduce
- Any suggested fix, if you have one
