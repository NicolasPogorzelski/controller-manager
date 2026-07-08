# Security Policy

## Scope

Controller Manager installs two root-owned helpers and a scoped NOPASSWD sudoers rule:

- `controller-hidraw-gate` — on `/dev/hidraw*` nodes of controllers on a built-in vendor
  allowlist it `chmod`s the nodes, manages marker files under a root-owned `/run`
  directory (names validated against a conservative charset), and unbinds/rebinds the
  node's kernel HID driver. It refuses any device outside the allowlist.
- `controller-led` — writes the lightbar / player LEDs of a Sony (`054C`) `:rgb:indicator`
  / `:white:player-N` LED only, refusing every other LED.

Both binaries are `0755` (not user-writable — a precondition for safe NOPASSWD) and
validate their target before touching anything; the sudoers rule is scoped to exactly
these two paths. If you find a way to bypass these constraints or to escalate privileges
through any part of this project, please report it privately.

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Report privately via GitHub: go to the
[Security tab](https://github.com/NicolasPogorzelski/controller-manager/security/advisories)
and choose **Report a vulnerability**. You will receive a response within 7 days.

Please include:
- A description of the vulnerability and its impact
- Steps to reproduce
- Any suggested fix, if you have one
