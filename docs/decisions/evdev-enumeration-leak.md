# Decision: The evdev enumeration leak (phantom pad in a dual remap)

## Context

With two DualSense pads both in `ps5-xbox` (Xbox emulation), Steam's
*Settings → Controller* list shows **five** controllers instead of four: the two
native Xbox pads (Bluetooth), the two emulated virtual Xbox pads (USB), and one extra
**PlayStation ("PS5") controller**. The phantom appears the moment the *second* pad is
emulated and is **position-, not device-specific**: whichever pad is emulated first is
listed correctly; emulating a second one always adds the fifth entry. One pad emulated =
four controllers, clean.

This is not a hidraw-gate failure. Field verification (2026-07-15), both pads emulated:

- Both physical DualSense hidraw nodes are `c---------` (`MODE 0000`) with **no** fd
  holder — the [hidraw gate](hidraw-gate.md) revoked Steam on both and holds. hidraw is
  not the leak.
- The leak is the physical **evdev** node. `lsof` on the second-emulated pad's
  `/dev/input/eventX` shows it held by **both** the daemon (its remapper) **and** `steam`;
  the first-emulated pad's event node is held by the daemon only. Steam reads the pad's
  VID/PID straight off that evdev node (`EVIOCGID` → `054c:0ce6`) and lists it as a
  PlayStation controller.
- The phantom is **inert**. `evtest` on the leaked node reports *"This device is grabbed
  by another process. No events are available"* — the remapper's `EVIOCGRAB` holds, so
  the physical pad's input still goes only to the virtual Xbox pad. There is **no double
  input**; the phantom is a dead list entry.

## Root cause

`EVIOCGRAB` gives the remapper exclusive access to the source's input **events**. It does
**not** remove the evdev node from the device tree or stop another process from *opening*
it: an open + `EVIOCGID` is enough for Steam to enumerate and list the pad, grabbed or not.

Why only the *second* pad leaks is a race the gate itself creates. A `ps5-xbox` transition
runs the gate's driver **rebind** (to revoke Steam's pre-open hidraw fd and reset the
lightbar latch — see [hidraw gate](hidraw-gate.md)). That rebind is device churn Steam
re-enumerates on: it re-opens every controller node it can, including the just-reborn evdev
node of the pad being emulated. For the *first* pad the remapper wins the open/grab race,
so Steam never keeps an fd on it. For the *second* pad, the second block's rebind triggers
a fresh full re-enumeration during which Steam re-opens the reborn evdev node before the
remapper grabs it — and because a grab does not evict an existing opener, Steam's fd sticks.
Hence: always the second, regardless of which physical pad it is.

## Why there is no lightweight robust fix

The obvious fix — extend the born-gated `MODE 0000` treatment from the hidraw node to the
evdev node — does not work here, for a structural reason:

- **The daemon is a `systemd --user` service; it and Steam run under the same UID**
  (`admin`). Its only privilege is NOPASSWD access to two scoped helpers
  (`controller-hidraw-gate`, `controller-led`).
- Steam's access to the evdev node comes from the **`uaccess` seat ACL** (`TAGS=…:uaccess:`
  → logind grants the active session user rw). The daemon is the *same* session user, so
  that identical ACL is also what lets the **remapper** open the node to grab it.
- Therefore any permission, group, or ACL barrier that denies Steam the evdev node denies
  the remapper too. Born-gating the node `MODE 0000` would lock out the daemon's own grab.
  A user service cannot be granted a supplementary group Steam lacks (same UID inherits the
  same group set), so no group split exists either.

Two lighter levers were considered and rejected:

- **SDL ignore-vars** (`SDL_GAMECONTROLLER_IGNORE_DEVICES=0x054c/0x0ce6`) would suppress the
  phantom but is already rejected project-wide: scoped to Steam it strips native DualSense
  features from Steam-launched games; see [Steam coexistence](steam-coexistence.md).
- **Stripping `ID_INPUT_JOYSTICK`** from the gated node via udev is unreliable: SDL falls
  back to reading the device's capability bitmaps from sysfs when the property is absent and
  re-derives "joystick" from `BTN_GAMEPAD`/`ABS_*`, so the classification returns anyway.

The only robust fix keeps the evdev node openable by the daemon while denying Steam — which,
given the same-UID constraint, requires a privileged path:

1. udev: born-gate the gated pad's **input/event** node as well (`SUBSYSTEM=="input"`,
   `uaccess` removed, `MODE 0000`), so Steam cannot open it.
2. a new NOPASSWD helper branch: open the evdev node as root, `EVIOCGRAB` it, and hand the
   fd to the daemon over a unix socket (`SCM_RIGHTS`). The grab is a property of the open
   file description, so it survives the helper's exit as long as the daemon holds the fd.
3. remapper: take the source fd from the helper instead of `evdev.InputDevice` (which only
   opens by path — it cannot adopt an fd), read raw `input_event` structs from it, and read
   the source capabilities from sysfs (`/sys/class/input/eventX/device/capabilities/*`,
   world-readable) to build the virtual pad.

## Decision

**Document the phantom as a known limitation and defer the robust fix.** The phantom is
inert — no double input, no feature broken — so it is a cosmetic list entry, and the only
robust fix expands a security-sensitive privileged surface (a helper that hands out grabbed
input fds of controllers) and rewrites the remapper's read path. That trade-off is not
justified by a purely cosmetic symptom.

**Escalation trigger.** Build the fd-passing fix (above) only if a real game shows
*functional* harm — the phantom stealing a player slot or shifting controller ordering in
local multiplayer. A game-side slot test is pending (planned, not yet run as of
2026-07-15). If the phantom is ignored in-game, documentation stands.

## Consequences

- No runtime behaviour changes; the daemon is untouched. Existing gate, remapper, and
  lightbar guarantees are unaffected.
- The full root cause, its verification evidence, and the fix design are captured so the
  deferred work can start from the decision, not a re-investigation — mirroring how the
  deferred concurrency item in the [roadmap](../../ROADMAP.md) is handled.
- User-facing symptom and the "is it inert?" verification live in
  [troubleshooting](../troubleshooting.md).

## Rejected alternatives

- **Best-effort race-retry** (after `apply_mode`, if Steam holds the evdev node, rebind +
  re-grab until Steam loses the open race): rejected. Each extra rebind renumbers nodes,
  churns the lightbar, and triggers *another* Steam re-enumeration — i.e. the same race
  again — with no guarantee of convergence, adding fragility to the most delicate part of
  the daemon for a cosmetic gain.
- **Running the daemon as root** (system service) so it can open a born-gated `MODE 0000`
  node while Steam cannot: rejected. It is a session/tray app on the D-Bus session bus, and
  running it as root is a far larger security downgrade than one scoped fd-passing helper.
