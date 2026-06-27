# Runbook: Update

Update an existing Controller Manager installation to a newer version from the repository.

## Preconditions

- The service is currently installed (`systemctl --user is-active controller-manager.service`).
- The repository is checked out locally with the target version available (e.g. `git pull`).
- Any application relying on an active remap can tolerate a brief interruption (the service
  restarts during the update).

## Steps

From the repository root:

```bash
./install.sh
```

`install.sh` is idempotent: it overwrites the daemon, service file, and root-owned helper
in place, then restarts the service. No manual cleanup is needed between versions.

If the sudoers rule changed in the new version, verify it after the install:

```bash
sudo visudo -c
```

## Verification

```bash
# Service is running after the update
systemctl --user is-active controller-manager.service   # → active

# Persisted modes are intact
cat ~/.config/controller-modes.json
```

The tray icon should reappear and list connected controllers with their previous modes.

## Failure modes

| Symptom | Check |
|---|---|
| Service is `failed` after update | `journalctl --user -u controller-manager.service -n 30` — usually a new Python dependency that is not installed |
| Persisted mode no longer appears in tray | The mode was removed in this version; the daemon falls back to native — expected behaviour |
| `sudo` prompt during install despite sudoers rule being present | The sudoers rule template changed; re-running `./install.sh` re-renders and re-installs it |

## Rollback

Check out the previous version and re-run `install.sh`:

```bash
git checkout <previous-commit-or-tag>
./install.sh
```

The mode configuration (`~/.config/controller-modes.json`) is keyed by the device's stable
`uniq` identifier and is forward- and backward-compatible across versions. It is never
modified by `install.sh` and does not need to be restored.
