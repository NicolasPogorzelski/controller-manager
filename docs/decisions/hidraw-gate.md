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

## Decision (v1, superseded): chmod the node

The first gate set the controller's `/dev/hidrawN` to mode `000` for the duration of a
remap and restored it afterwards. That closed the *enumeration* path but had a documented
hole: **a process that already holds the hidraw fd keeps it across a later `chmod`**.

The hole turned out to be the normal case, not the edge case. Steam Input opens every
pad's hidraw node the moment it connects — seconds before any tray click can gate it. The
already-open fd let Steam keep reading the physical pad (double input) *and* keep writing
output reports: on a DualSense, Steam sets the lightbar-setup "light out" flag, after
which the pad firmware ignores plain colour writes — including the kernel LED-class
writes the daemon uses. The lightbar stayed dark no matter what the daemon painted, even
after Steam exited, because the latch lives in the pad until the driver re-probes.

## Decision (v2): identity-keyed gate + born-gated nodes + driver rebind

Gating is keyed by the pad's **stable HID identity** (`HID_UNIQ`: BT MAC / serial), not a
volatile node, and consists of three cooperating parts:

1. **Marker file** `/run/controller-manager/gated/<uniq>` — the gate state. `/run` is a
   tmpfs, so markers cannot survive a reboot: the system always boots fail-open.
2. **udev rule** (`72-controller-manager.rules`) — on every hidraw `add` event of a
   Sony/Microsoft pad it asks the helper (`udev-check`) whether the parent HID device is
   gated; if so the node is **born** with `MODE :=0000` (final assignment — no later rule
   can override it) and, crucially, **without the `uaccess` tag**. The filename `72-`
   places it after the whole `71-*-controllers.rules` family (steam-devices re-adds
   `MODE 0660` + `uaccess` for Sony pads there — verified in the field to defeat the gate
   when this rule still ran at `71-`) and before `73-seat-late.rules` (which turns the
   tag into a seat-user ACL) — without the tag removal, the desktop user's processes
   (Steam) would pass straight through `MODE 0000` via the ACL. As belt and braces the
   helper re-chmods the reborn nodes after the rebind, which also zeroes the mask of any
   ACL that ever leaks through again.
3. **Driver rebind** — on a gate *transition* the helper unbinds and rebinds the kernel
   HID driver. That destroys **every** open fd on the old nodes (revoking Steam's
   pre-gate handle) and re-probes the pad, which also resets the DualSense lightbar
   latch. The recreated nodes are then born gated (rule above), so nothing can re-open
   them first.

Block/restore rebind **only on a state transition** (marker created / removed), never on
a repeated call: the daemon re-asserts the gate after the rebind's own device churn, and
rebinding again on that re-assert would loop forever. A separate `rearm` subcommand
rebinds without touching the gate — used by the resting-colour policy (see
[Steam coexistence](steam-coexistence.md)).

Pads that report no `HID_UNIQ` fall back to the v1 per-node chmod
(`block-node`/`restore-node`) — best effort, no revocation.

The daemon calls `sudo -n controller-hidraw-gate <block|restore|rearm> <uniq>`. If the
helper or rule is absent, the call is a silent no-op (the daemon still runs, without
gating).

## Consequences

- **Launcher-agnostic and pre-open-proof.** The gate hides the controller from every
  consumer at once, *including* processes that opened the node before the gate — the
  rebind revokes them. Applications no longer need to be started in a particular order or
  restarted.
- **Per-device, not per-model.** The gate targets one physical pad's identity, so two
  identical controllers are gated independently: one remapped (gated) while the other
  stays native (open).
- **Device nodes renumber on a gate transition.** The rebind tears down and recreates the
  pad's input/hidraw nodes; the daemon re-resolves its bindings afterwards
  (`_refresh_nodes`) before grabbing.
- **Gates survive reconnects by design.** The gate is keyed by identity, so a
  Bluetooth reconnect of a remapped pad comes back *born gated* — there is no window in
  which another process can capture the pad.
- **Controllers without a hidraw node** (e.g. bound by a kernel driver that exposes no
  raw HID node) need no gate — the evdev grab alone suffices, and the gate is a no-op
  there.
- **Fail-safe restore.** Stopping an instance (disconnect or shutdown) always ungates,
  and a reboot clears all markers — a gate never lingers.

## Security notes

The privileged surface is one root-owned binary (`0755`, not user-writable — a
precondition for safe NOPASSWD) that only acts on HID devices of a built-in vendor
allowlist: it chmods their hidraw nodes, manages marker files in a root-owned `/run`
directory (marker names are validated against a conservative charset so they cannot
escape it), and unbinds/rebinds their kernel driver. The sudoers rule
(`/etc/sudoers.d/controller-hidraw`, `0440`) is scoped to that single binary, so the
NOPASSWD grant cannot be repurposed. `udev-check` is invoked by udevd directly (already
root) and only performs a file-existence test.
