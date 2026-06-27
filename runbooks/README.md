# Runbooks

Operational procedures for installing and verifying Controller Manager.

**Runbook contract (applies to every runbook):**

- **Preconditions** — what must be true before starting.
- **Steps** — copy/paste-safe commands in deterministic order.
- **Verification** — how to confirm success.
- **Failure modes** — common errors and what to check next.
- **Rollback** — how to undo, where applicable.

## Index

- [Installation](install.md) — deploy the daemon, service, and hidraw gate.
- [Update](update.md) — update an existing installation to a newer version.
- [Verify a remap](verify-remapping.md) — confirm grab, virtual device, and gating.
