# Roadmap

Short, rolling list of where this project stands and what comes next. Newest status on top.

## Status — 2026-07-06

Multi-controller track started; the 2× DualSense half is **verified on hardware**
(independent tray entries and modes, per-pad gate, click routing, stable numbering).
2× Xbox still pending. Landed on the way:

- **Scan filter**: our own virtual pads are now recognised by python-evdev's uinput
  phys marker instead of by name — xpad names a REAL wired Xbox 360 pad exactly like
  our virtual target, so such pads were never adopted. Tests: `tests/test_multi_pad.py`.
- **Adoption latch fix**: a pad already held by Steam when adopted (freshly paired
  identity → no gate marker → the adoption restore rebound nothing) kept Steam's
  connect-time "light out" firmware latch, so every kernel LED write was a firmware
  no-op. Adoption now force-rearms when foreign holders pre-date it, and every
  apply_mode arms a one-shot ~6 s repaint (the write straight after a rebind races the
  BT re-probe and can be dropped — also in remap modes, which previously never
  repainted). Tests: `tests/test_lightbar_latch.py`.
- **Stable player LEDs**: daemon-assigned player numbers in connection order, persisted
  under `_players` in `controller-modes.json`, asserted on the DualSense white player
  LEDs and re-asserted after every gate churn (Steam renumbers its slots on every
  rebind and writes them raw — numbers used to rotate with mode switches). Tray labels
  show the same number. Verified on hardware with both pads.
  Decision: `docs/decisions/player-leds.md`.
- **Lightbar "light out" latch — root-caused and solved** (a hardware defect was
  suspected at first; disproven): the Steam client opens every DualSense hidraw at
  startup for identification even with PlayStation *and* Xbox controller support
  disabled, repaints the pads with its slot colours and leaves the firmware latch
  behind on exit — from then on kernel LED writes land but light nothing until a
  driver rebind. The classic `controller_blacklist` in `config/config.vdf` proved
  **ineffective**; what works is the SDL ignore-list environment variables scoped to
  Steam via a user-local `steam.desktop` override. Both pads verified blue on
  hardware with Steam running. Details and trade-offs:
  `docs/decisions/steam-coexistence.md` (field finding 2026-07-06).

## Status — 2026-07-05

Root causes of the three field bugs found, fixed, unit-tested and **verified on
hardware** with Steam running throughout (merged to `main` via PR #3; Issue #2 closed):

- **Tray menu items stuck disabled / clicks dead after reconnect churn (Issue #2).**
  Verified live: the daemon served `enabled=true` while the GNOME appindicator host (the
  desktop is GNOME, not Plasma as the issue hypothesised) rendered the item disabled —
  the host caches item properties per dbusmenu id and misses reused ids across
  structural changes. Fix: cached menu model, never-recycled ids, ItemsPropertiesUpdated
  deltas. Decision: `docs/decisions/tray-menu-model.md`; test: `tests/test_menu_ids.py`.
- **DualSense lightbar dark despite correct kernel LED state.** Steam Input holds the
  pad's hidraw fd from connect (survives the chmod gate) and firmware-latches the
  lightbar off. Fix: gate v2 — identity-keyed markers, udev-born-gated nodes, driver
  rebind to revoke fds and reset the latch; resting-blue policy in native mode.
  Decisions: `docs/decisions/hidraw-gate.md`, `docs/decisions/steam-coexistence.md`.
- Also: Sunshine/inputtino virtual pads excluded from the scan, identical uniq-less pads
  kept apart via phys, remapper liveness re-assert (closes the sub-poll-blip backlog
  item).

## Next up

- Multi-controller verification, remaining half: + 2× Xbox simultaneously (the
  2× DualSense half is verified, see 2026-07-06). Runbook:
  `runbooks/verify-multi-controller.md`.
- Protocol research for broader transport support (Xbox Wireless dongle, GIP, …).
- **Xbox → DualSense output via uhid** (real hidraw node with a DualSense report
  descriptor) — the prerequisite for offering `xbox-ps5`; see
  `docs/decisions/output-protocol-constraints.md`.

## Status — 2026-07-02

Bluetooth reconnect robustness for the DualSense lightbar + remap is **done and verified
on hardware** (merged to `main`, `91bb4e3`). On a BT reconnect the `/dev/input/eventX` path
is reused while `inputN`-derived nodes (the `:rgb:indicator` LED) renumber. The daemon now:

- re-resolves the LED node from the live evdev path on every write (never trusts a cached name),
- detects a reconnect by a *presence transition* (not a path change) and re-asserts the whole mode,
- repaints the lightbar on a bare node renumber.

## Next up — 2026-07-03

- **Issue #2 — tray menu clicks stop reaching the daemon after reconnect churn.**
  Plasma StatusNotifierItem / `com.canonical.dbusmenu` host-side sync issue: after several
  reconnects, tray radio clicks are not delivered to our object, while the same `Event` sent
  over `dbus-send` works; restarting the service re-registers the item and restores it.
  First steps: capture the host side with `dbus-monitor` during a failing click; test whether
  re-issuing `RegisterStatusNotifierItem` or emitting a `LayoutUpdated` bump on reconnect
  nudges the host to re-bind. Full write-up and evidence: GitHub Issue #2.

## Backlog / known limitations

- **LED write race vs Steam Input (residual):** a single LED write at the exact instant of
  reconnect can be overwritten as the pad comes up; the re-assert one poll later is what makes
  the colour stick — best-effort, not a hard guarantee. Not a new bug.
- **Remapper liveness on sub-poll blips:** the reconnect re-assert triggers on an absent→present
  transition seen by the 2 s poll. A BT blip shorter than one poll can kill the remapper's grab
  without the poll noticing, leaving the pad un-remapped until the next real event. A liveness
  check (restart the remapper if the mode wants one but it is not alive) would close this.
