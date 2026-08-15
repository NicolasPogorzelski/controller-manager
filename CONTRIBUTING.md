# Contributing

Thanks for your interest in improving Controller Manager. This is a small project; the
goal is to keep it understandable and reliable rather than feature-rich.

## Development setup

The daemon runs from the repository, so the fastest loop is to edit and redeploy:

```bash
# Deploy the daemon only and restart the service
install -m 0755 controller-manager.py ~/.local/bin/controller-manager.py
systemctl --user restart controller-manager.service

# Follow the logs
journalctl --user -u controller-manager.service -f
```

A full deploy (daemon, service, and the root-owned hidraw gate) is done with
`./install.sh`. See the [installation runbook](runbooks/install.md).

To confirm a change to the remapping path actually works, follow the
[verification runbook](runbooks/verify-remapping.md); for changes touching multi-controller
behaviour (numbering, per-pad gating, reconnect isolation) use the
[multi-controller runbook](runbooks/verify-multi-controller.md).

## Code

- Target Python 3.9+ and the standard desktop stack (`python-evdev`, `dbus-python`,
  `PyGObject`). Avoid adding dependencies.
- Match the surrounding style: section banners, terse comments that explain *why*, and
  the existing naming.
- Syntax-check before committing:

  ```bash
  python3 -m py_compile controller-manager.py
  ```

## Commit messages

Conventional Commits with a scope are required:

```
<type>(<scope>): <description>
```

Types: `feat`, `fix`, `docs`, `refactor`, `chore`, `test`, `ci`.
Scopes: `manager`, `gate`, `service`, `install`, `docs`, `ci`, `repo`.

Check a message with:

```bash
./scripts/commit-msg-lint.sh "fix(manager): wake the remapper loop on stop"
```

To enforce the format automatically on every commit, install the script as a hook:

```bash
ln -s ../../scripts/commit-msg-lint.sh .git/hooks/commit-msg
```

Do not include AI/assistant attribution trailers in commit messages.

## Documentation

- Architecture and rationale go under `docs/`. Significant design choices are recorded as
  short decision documents in `docs/decisions/` (context -> decision -> consequences).
- Operational procedures go under `runbooks/`. Every runbook follows the contract in
  [runbooks/README.md](runbooks/README.md): **Preconditions, Steps, Verification, Failure
  modes**, and **Rollback** where applicable.
- Keep everything generic and reusable - no machine-specific paths or personal
  environment details.

## Before opening a pull request

Run the validation script; it also runs in CI:

```bash
./scripts/validate-repo.sh
```
