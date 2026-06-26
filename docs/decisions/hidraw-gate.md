# Decision: The hidraw Gate

## Context

A remapped controller should be invisible to applications under its physical identity —
only the virtual device should be seen. An `EVIOCGRAB` on the `evdev` node was expected to
guarantee that.

It does not. Two access paths exist to the same controller:

- the **evdev** node (`/dev/input/eventX`), which the grab covers, and
- the raw **HID** node (`/dev/hidraw*`), which it does not.

Some applications read game controllers straight from `/dev/hidraw*` (a common path
through compatibility layers that use a HID API). In a remap mode this produces **double
input**: the application sees both the virtual device and the still-readable physical
device, and may identify the controller by its physical identity.

**Symptom → diagnosis.** With a remap active, an application reacts to doubled input and
shows the wrong device glyphs. `lsof` on the nodes makes the cause explicit:

```
event24 (physical evdev)  → held only by the daemon          ← grab works
hidraw9 (physical hidraw) → held by an application process    ← leak
```

The evdev grab is working; the raw HID path is the leak. An SDL-style "ignore device" hint
does not help either — it only affects that library's enumeration, while the raw HID node
is read underneath it.

## Decision

Gate the **raw HID node** for the duration of a remap. When a controller enters a remap
mode, its `/dev/hidrawN` is set to mode `000` (inaccessible to all processes); native mode,
disconnect, and shutdown restore it to `666`.

`chmod` on a device node requires root, so the daemon delegates to a tightly scoped helper:

- **`/usr/local/bin/controller-hidraw-gate`** — root-owned, `0755` (not user-writable, a
  precondition for safe NOPASSWD). It only ever `chmod`s `/dev/hidraw*` nodes, and only for
  controllers on a built-in allowlist. It resolves the node from the HID id in
  `/sys/class/hidraw/*/device/uevent`.
- **`/etc/sudoers.d/controller-hidraw`** (`0440`) — a single NOPASSWD rule for that helper
  and nothing else.

The daemon calls `sudo -n controller-hidraw-gate <block|restore> <node>`. If the helper or
rule is absent, the call is a silent no-op (the daemon still runs, without gating).

## Consequences

- **Launcher-agnostic.** Because the gate changes a system-wide device property, it hides
  the controller from every consumer at once — no per-application configuration.
- **Per-device, not per-model.** The gate targets a specific hidraw node, so two identical
  controllers are gated independently: one can be remapped (gated) while the other stays
  native (open).
- **Must precede the application.** The gate must be active before an application opens the
  node. A process that already holds the hidraw fd keeps it across a later `chmod`; in that
  case the application must be restarted.
- **Controllers without a hidraw node** (e.g. those bound by a kernel driver that exposes
  no raw HID node) need no gate — the evdev grab alone suffices, and the gate is a no-op
  there.
- **Fail-safe restore.** Stopping an instance (disconnect or shutdown) always restores the
  node, so a gate never lingers after the daemon or controller goes away.

## Security notes

The privileged surface is one root-owned binary that only `chmod`s allowlisted hidraw
nodes. The sudoers rule is scoped to that single binary, and the binary is not writable by
the invoking user — so the NOPASSWD grant cannot be repurposed.
