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
| Sudoers rule | `/etc/sudoers.d/controller-hidraw` | scoped NOPASSWD for the gate only |

## Detection and the device model

`ControllerManager` runs a monitor thread that scans `evdev.list_devices()` every two
seconds. A device is treated as a controller when:

1. its **vendor id** is known (`0x054c` → PlayStation, `0x045e` → Xbox), and
2. it advertises a **gamepad capability** — either `BTN_GAMEPAD` or `BTN_SOUTH` (whichever the driver exposes).

Recognising by vendor plus capability — rather than a fixed product-ID list — means
unlisted models of a known vendor still work, and non-gamepad nodes that share a vendor id
(headset, motion sensor, touchpad) are excluded. Product IDs are used only to choose a
nicer display name.

Each detected device becomes a `ControllerInstance`, keyed by its `/dev/input/eventX`
path. The instance carries the device's **stable identity** (`uniq`, e.g. the Bluetooth
MAC), which is what configuration and the tray label are keyed on — see
[per-device identity](../decisions/per-device-identity.md).

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
