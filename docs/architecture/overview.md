# Architecture Overview

Controller Manager is a single user-space daemon (`controller-manager.py`) plus a small
root-owned helper (`controller-hidraw-gate`). The daemon detects controllers, applies a
per-device mode, and serves a tray menu over D-Bus. The helper performs the one privileged
action the daemon needs: gating raw HID device nodes.

```
                         controller-manager.py (systemd --user)
                         ┌──────────────────────────────────────────────┐
 /dev/input/event*  ───► │ ControllerManager   (detect, hotplug, modes)  │
                         │   └─ ControllerInstance (one per device)       │
                         │        └─ Remapper thread (grab + uinput)      │
                         │                                                │
 D-Bus (session) ◄─────► │ TrayIcon (StatusNotifierItem)                  │
                         │ DbusmenuServer (com.canonical.dbusmenu)        │
                         └───────────────┬────────────────────────────────┘
                                         │ sudo -n (NOPASSWD, scoped)
                                         ▼
                         controller-hidraw-gate  ──► chmod /dev/hidraw*
```

## Component map

| Component | Path (installed) | Responsibility |
|---|---|---|
| Daemon | `~/.local/bin/controller-manager.py` | detection, remapping, tray, hotplug |
| Service | `~/.config/systemd/user/controller-manager.service` | autostart with the desktop session |
| Config | `~/.config/controller-modes.json` | persisted mode per device |
| HID gate | `/usr/local/bin/controller-hidraw-gate` | privileged `chmod` of `/dev/hidraw*` |
| LED helper | `/usr/local/bin/controller-led` | privileged write of a Sony `:rgb:indicator` lightbar |
| Sudoers rule | `/etc/sudoers.d/controller-hidraw` | scoped NOPASSWD for the two helpers |

## Detection and the device model

`ControllerManager` runs a monitor thread that scans `evdev.list_devices()` every two
seconds. A device is treated as a controller when:

1. its **vendor id** is known (`0x054c` → PlayStation, `0x045e` → Xbox), and
2. it advertises a **gamepad capability** — either `BTN_GAMEPAD` or `BTN_SOUTH` (whichever the driver exposes).

Recognising by vendor plus capability — rather than a fixed product-ID list — means
unlisted models of a known vendor still work, and non-gamepad nodes that share a vendor id
(headset, motion sensor, touchpad) are excluded. Product IDs are used only to choose a
nicer display name.

Each detected device becomes a `ControllerInstance`, keyed by its **stable identity**
(`uniq`, e.g. the Bluetooth MAC; else `vendor:product`) — the same key configuration and the
tray label use, see [per-device identity](../decisions/per-device-identity.md). Keying on
identity rather than the `/dev/input/eventX` path matters because a Bluetooth pad
re-registers on a *new* evdev/hidraw node after every reconnect (e.g. when it is un/re-paired
for console use): the reconcile pass recognises the same identity and **rebinds** the instance
to the new node in place — restarting only the remapper — instead of tearing it down and
rebuilding it, which would churn the tray menu into stuck-disabled items. The same re-assert
also runs when a pad returns on a *reused* `eventX` path (detected as a previously-absent
instance reappearing, not a path change): the device underneath — and thus its remapper grab
and lightbar — is new even though the path number is not. A pad that vanishes is dropped only
after a short grace period, so a brief reconnect blip never removes it.

## Modes

| Mode | Family | Effect |
|---|---|---|
| `ps5-native` | PlayStation | pass-through, no grab |
| `ps5-xbox` | PlayStation | grab + emit a virtual Xbox 360 pad |
| `xbox-native` | Xbox | pass-through, no grab |

The modes offered per family are defined by `MODES_FOR_FAMILY`. The Xbox→PlayStation
direction is deliberately omitted (see
[output protocol constraints](../decisions/output-protocol-constraints.md)). A persisted
mode that is no longer offered for a family falls back to that family's native default, so
removing a mode never leaves a controller stuck applying it.

## The remapper

A remap runs in its own `Remapper` thread. On start it:

1. opens the source device and copies its capabilities (dropping `EV_SYN`/`EV_FF`);
2. creates a **`uinput`** virtual device with the target identity (`VIRTUAL_XBOX`);
3. grabs the source with `EVIOCGRAB` for exclusive access;
4. forwards `EV_KEY` / `EV_ABS` / `EV_REL` events to the virtual device.

Two details matter:

- **Translated capabilities are advertised.** Some controllers expose a non-standard
  evdev button layout. The remapper carries a per-source quirk map and advertises the
  *translated* (standard) key codes on the virtual device, so the consuming side maps the
  target identity against standard codes rather than the source's raw ones.
- **The loop is interruptible.** The event loop uses `selectors` over the source fd plus a
  self-pipe. `stop()` writes to the pipe, waking the loop immediately even when the
  controller is idle. This replaced an earlier design that closed the source fd from
  another thread — see [the remapping engine decision](../decisions/remapping-engine.md)
  for why that leaked.

The virtual device is created before the grab is attempted; if the grab fails, the virtual
device is closed so it cannot linger. Virtual devices are excluded from the detection scan
(by name and by path) so the daemon never tries to remap its own output.

## The hidraw gate

An `evdev` grab hides a device only on the evdev layer. Some applications read controllers
straight from `/dev/hidraw*`, bypassing the grab, which causes double input in a remapped
mode. When a controller enters a remap mode the daemon asks the helper to `chmod` that
controller's **specific** hidraw node to `000`; native mode restores it to `666`. Because
the gate targets a node (not a vendor:product pair), two identical controllers are gated
independently. Full rationale: [the hidraw gate](../decisions/hidraw-gate.md).

## The lightbar (DualSense)

A DualSense mode is signalled on the controller's lightbar: blue for `ps5-native`, green
for `ps5-xbox`. The colour is driven through the kernel **LED class** — the
hid-playstation `…:rgb:indicator` multicolor node, written by the `controller-led` helper —
not via raw hidraw output reports, which race the driver and get dropped over Bluetooth
(leaving the lightbar on its firmware default). The kernel does the USB/BT report framing,
so the colour is reliable.

The lightbar is fragile across reconnects for two reasons, and the daemon defends against
both:

- **The LED node renumbers.** Its name derives from the `inputN` instance counter, which
  bumps on *every* reconnect (`input38` → `input47` → …) — even when the `/dev/input/eventX`
  path is reused, so a path-keyed check would miss it. `_apply_led()` therefore never trusts
  a cached node name: it re-resolves the `:rgb:indicator` node from the live evdev path on
  every write, so a colour can never land on a stale, now-dead node.
  `ControllerInstance.refresh_led()` (run each monitor tick, outside the reconcile lock
  because the write may shell out to `sudo`) repaints when the live node differs from the one
  last painted — covering a renumber that happens without a polled absence.
- **The firmware resets the lightbar on power-cycle.** A pad toggled off/on returns on its
  firmware-default blue and un-remapped (its old grab died with the disconnect). Because the
  evdev path is often reused, the daemon detects such a reconnect by a *presence transition*
  (a previously absent instance reappearing) rather than a path change, and re-asserts the
  whole mode — restarting the remapper, re-gating the hidraw node and repainting the lightbar.

A single write issued at the exact instant of reconnect can still be overwritten by the
device / Steam Input as it comes up; re-asserting one poll later, once the pad is stably
present, is what makes the colour stick.

## The tray

The tray is a `StatusNotifierItem` served directly over D-Bus, with its menu provided by a
`com.canonical.dbusmenu` object — no GUI toolkit is linked. The item is registered with
the `StatusNotifierWatcher` using its **object path**, and the menu is rebuilt on every
change (mode switch or hotplug). Each connected controller becomes a section of radio
items; selecting one calls back into `ControllerManager.set_mode()`.

## Lifecycle and persistence

- **Hotplug:** the monitor adds instances for new devices and stops instances for removed
  ones; stopping always restores the hidraw node so a gate never lingers after a
  disconnect.
- **Persistence:** `set_mode()` writes the chosen mode to `controller-modes.json`, keyed
  by the device's `uniq`.
- **Shutdown:** on `SIGTERM`/`SIGINT` the daemon stops every instance, releasing grabs and
  restoring all gated nodes.
