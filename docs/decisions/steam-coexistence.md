# Decision: Coexistence with Steam Input (and other raw-HID consumers)

## Context

Steam Input opens every controller's `/dev/hidraw*` node the moment it connects — even
with no game running, minimized to the tray. Through that handle it reads input and
writes output reports; on a DualSense it routinely sets the lightbar-setup **"light
out"** flag, a state that lives in the pad's firmware: from then on plain colour writes
(including the kernel LED-class writes this daemon uses) change nothing until the driver
re-probes the pad. Games with native DualSense support (SDL hidapi — most modern titles,
under Steam, Lutris or bare Wine/Proton alike) behave the same way while they run.

So there are two legitimate writers for one lightbar, and "the daemon's status colour"
and "the game's lightbar effects" cannot both win at the same time.

## Ownership model per mode

| Mode | Raw HID (hidraw) | Lightbar |
|---|---|---|
| `ps5-xbox` (remap) | daemon exclusive — the [gate](hidraw-gate.md) revokes and blocks everyone else | **green, guaranteed** |
| `ps5-native` | applications (Steam, games) — full native features, daemon hands off | **resting blue**, games may override while they hold the pad |

## Decision: resting colour, not a paint war

In `ps5-native` the daemon must not fight applications for the lightbar (a periodic
repaint would flicker against game effects and defeat native support). Instead it watches
**who holds the pad's hidraw nodes** (`/proc/*/fd` — same-user processes, which is
exactly the Steam/games population):

- someone holds the pad → the lightbar is legitimately theirs; hands off;
- the **last holder disappears** (game exited, Steam closed) → the pad may be latched
  dark, so the daemon *rearms* it (driver rebind → fresh lightbar setup) and repaints the
  resting blue within a couple of monitor ticks.

Games that never touch raw HID (plain evdev/joystick titles) never take the lightbar, so
the resting blue simply stays.

## Required setup: disable PlayStation *and* Xbox controller support in the Steam client

While the Steam *client* runs with PlayStation controller support enabled, it holds the
pad permanently — indistinguishable from a running game, so the resting colour can never
return until Steam exits. The supported setup is therefore:

**Steam → Settings → Controller → PlayStation controller support: off.**

Consequences of that setting:

- The Steam client leaves the physical DualSense alone; the resting blue works.
- Games keep their **native** DualSense features — modern titles talk to the pad through
  their own SDL/hidapi, not through the Steam client.
- What is lost is Steam-Input *remapping of the physical DualSense identity* — which this
  project replaces anyway: switch the pad to `ps5-xbox` and Steam Input sees a plain
  Xbox 360 pad it can remap freely.

Without the setting, everything still degrades gracefully: green in `ps5-xbox` stays
guaranteed (the gate revokes Steam), and blue returns whenever Steam fully exits.

Steam Input's controller support is per-VID/PID family, not per-application, and it does
not care whether a device is physical or `uinput`-virtual: enabling **Xbox controller
support** makes Steam hold `ps5-xbox`'s virtual Xbox pad the same way PlayStation support
holds the physical DualSense. Confirmed in the field (2026-07-06, Fedora/GNOME notebook,
*Secrets of Grindea* under Steam): with Xbox controller support on, Steam opened the
virtual pad's evdev node (visible in `lsof`) but never passed a working controller through
to the game — input was dead in-game even though the daemon's remap was independently
verified correct at the event level. The virtual pad has no hidraw node (see
[output-protocol-constraints.md](output-protocol-constraints.md)), which Steam's Xbox
input path appears to need for full negotiation; lacking it, Steam swallows the device
without emulating it. Disabling **Xbox controller support** fixed it immediately, no
daemon-side change needed.

**Steam → Settings → Controller → Xbox controller support: off**, in addition to the
PlayStation toggle above, is therefore required for `ps5-xbox` to reach games running
under Steam.

## Rejected alternatives

- **Enforcing blue periodically in native mode** — visible flicker war against Steam and
  game effects; breaks the features native mode exists for.
- **Reading the current lightbar colour to decide** — impossible: raw-HID writes by other
  processes do not go through the kernel LED class, so there is nothing to read back.
