# Roadmap

Short, rolling list of where this project stands and what comes next. Newest status on top.

## Status - 2026-07-15: phantom pad in a dual remap - root-caused, deferred

With two DualSense both in `ps5-xbox`, Steam lists a **fifth, phantom** PlayStation pad -
appearing the moment the *second* pad is emulated, always the second regardless of which
physical pad. **Root-caused on hardware and confirmed harmless:** the hidraw gate holds on
both pads (both nodes `000`, no holder); the phantom is the second pad's **evdev** node,
which Steam re-opens and lists during the second `ps5-xbox` transition's driver rebind. The
remapper's `EVIOCGRAB` still holds it, so it is **inert** - `evtest` reports the node
grabbed, no events leak, no double input.

`EVIOCGRAB` hides events, not enumeration, and no lightweight robust fix exists: the daemon
is a `--user` service sharing Steam's UID, so any evdev permission/ACL barrier that blocks
Steam also blocks the remapper's own grab. **Decision: document and defer** - the phantom is
cosmetic, and the only robust fix (born-gate the evdev node + a root helper that hands the
grabbed fd to the daemon via `SCM_RIGHTS` + a raw-read remapper path) expands a
security-sensitive privileged surface for a dead list entry. A game-side slot test is
pending; escalate only if the phantom proves functionally harmful (stolen player slot /
reordered controllers). Full analysis: `docs/decisions/evdev-enumeration-leak.md`;
symptom + verification: `docs/troubleshooting.md`.

## Status - 2026-07-06 (evening): full Steam ownership in native mode

The SDL ignore-vars workaround from the afternoon (below) is **superseded**: the
project moved to the other clean end of the spectrum - the Steam client manages the
physical DualSense fully. PlayStation controller support is back on ("Enabled in
Games w/o Support", `PSSupport=1`): titles with native DualSense support get the pad
directly (adaptive triggers, gyro, haptics), everything else gets Steam Input. The
`steam.desktop` env override and the (ineffective anyway) `controller_blacklist`
entry are gone; Steam autostarts with the session (`~/.config/autostart/`).

Daemon-side, the resting-colour policy no longer counts holders (the client now holds
every pad permanently - the delta model could not tell an idle desktop from a running
game) but **classifies**: a `/proc` sweep for the launch wrapper's
`SteamLaunch AppId=` cmdline marker catches Steam Input titles, a holder-comm check
catches direct-access titles (Lutris, per-game Steam Input opt-outs). No game ->
the mode colour is re-asserted (event-driven one-shots + a slow backstop), so the pad
shows its mode at a glance right up to the moment a game starts and again seconds
after it ends. In `ps5-xbox` the green stays guaranteed even in-game (the gate keeps
the physical pad exclusive; games see only the virtual pad). Tests:
`tests/test_steam_ownership.py`; decision rewrite: `docs/decisions/steam-coexistence.md`.

## Status - 2026-07-06

Multi-controller track started; the 2x DualSense half is **verified on hardware**
(independent tray entries and modes, per-pad gate, click routing, stable numbering).
2x Xbox still pending. Landed on the way:

- **Scan filter**: our own virtual pads are now recognised by python-evdev's uinput
  phys marker instead of by name - xpad names a REAL wired Xbox 360 pad exactly like
  our virtual target, so such pads were never adopted. Tests: `tests/test_multi_pad.py`.
- **Adoption latch fix**: a pad already held by Steam when adopted (freshly paired
  identity -> no gate marker -> the adoption restore rebound nothing) kept Steam's
  connect-time "light out" firmware latch, so every kernel LED write was a firmware
  no-op. Adoption now force-rearms when foreign holders pre-date it, and every
  apply_mode arms a one-shot ~6 s repaint (the write straight after a rebind races the
  BT re-probe and can be dropped - also in remap modes, which previously never
  repainted). Tests: `tests/test_lightbar_latch.py`.
- **Stable player LEDs**: daemon-assigned player numbers in connection order, persisted
  under `_players` in `controller-modes.json`, asserted on the DualSense white player
  LEDs and re-asserted after every gate churn (Steam renumbers its slots on every
  rebind and writes them raw - numbers used to rotate with mode switches). Tray labels
  show the same number. Verified on hardware with both pads.
  Decision: `docs/decisions/player-leds.md`.
- **Lightbar "light out" latch - root-caused and solved** (a hardware defect was
  suspected at first; disproven): the Steam client opens every DualSense hidraw at
  startup for identification even with PlayStation *and* Xbox controller support
  disabled, repaints the pads with its slot colours and leaves the firmware latch
  behind on exit - from then on kernel LED writes land but light nothing until a
  driver rebind. The classic `controller_blacklist` in `config/config.vdf` proved
  **ineffective**; what works is the SDL ignore-list environment variables scoped to
  Steam via a user-local `steam.desktop` override. Both pads verified blue on
  hardware with Steam running. Details and trade-offs:
  `docs/decisions/steam-coexistence.md` (field finding 2026-07-06).

## Status - 2026-07-05

Root causes of the three field bugs found, fixed, unit-tested and **verified on
hardware** with Steam running throughout (merged to `main` via PR #3; Issue #2 closed):

- **Tray menu items stuck disabled / clicks dead after reconnect churn (Issue #2).**
  Verified live: the daemon served `enabled=true` while the GNOME appindicator host (the
  desktop is GNOME, not Plasma as the issue hypothesised) rendered the item disabled -
  the host caches item properties per dbusmenu id and misses reused ids across
  structural changes. Fix: cached menu model, never-recycled ids, ItemsPropertiesUpdated
  deltas. Decision: `docs/decisions/tray-menu-model.md`; test: `tests/test_menu_ids.py`.
- **DualSense lightbar dark despite correct kernel LED state.** Steam Input holds the
  pad's hidraw fd from connect (survives the chmod gate) and firmware-latches the
  lightbar off. Fix: gate v2 - identity-keyed markers, udev-born-gated nodes, driver
  rebind to revoke fds and reset the latch; resting-blue policy in native mode.
  Decisions: `docs/decisions/hidraw-gate.md`, `docs/decisions/steam-coexistence.md`.
- Also: Sunshine/inputtino virtual pads excluded from the scan, identical uniq-less pads
  kept apart via phys, remapper liveness re-assert (closes the sub-poll-blip backlog
  item).

## Next up

- Multi-controller verification, remaining half: + 2x Xbox simultaneously (the
  2x DualSense half is verified, see 2026-07-06). Runbook:
  `runbooks/verify-multi-controller.md`.
- Protocol research for broader transport support (Xbox Wireless dongle, GIP, ...).
- **Xbox -> DualSense output via uhid** (real hidraw node with a DualSense report
  descriptor) - the prerequisite for offering `xbox-ps5`; see
  `docs/decisions/output-protocol-constraints.md`.

## Status - 2026-07-02

Bluetooth reconnect robustness for the DualSense lightbar + remap is **done and verified
on hardware** (merged to `main`, `91bb4e3`). On a BT reconnect the `/dev/input/eventX` path
is reused while `inputN`-derived nodes (the `:rgb:indicator` LED) renumber. The daemon now:

- re-resolves the LED node from the live evdev path on every write (never trusts a cached name),
- detects a reconnect by a *presence transition* (not a path change) and re-asserts the whole mode,
- repaints the lightbar on a bare node renumber.

## Next up - 2026-07-03

- **Issue #2 - tray menu clicks stop reaching the daemon after reconnect churn.**
  Plasma StatusNotifierItem / `com.canonical.dbusmenu` host-side sync issue: after several
  reconnects, tray radio clicks are not delivered to our object, while the same `Event` sent
  over `dbus-send` works; restarting the service re-registers the item and restores it.
  First steps: capture the host side with `dbus-monitor` during a failing click; test whether
  re-issuing `RegisterStatusNotifierItem` or emitting a `LayoutUpdated` bump on reconnect
  nudges the host to re-bind. Full write-up and evidence: GitHub Issue #2.

## Backlog / known limitations

- **LED write race vs Steam Input (residual):** a single LED write at the exact instant of
  reconnect can be overwritten as the pad comes up; the re-assert one poll later is what makes
  the colour stick - best-effort, not a hard guarantee. Not a new bug.
- **Lightbar reclaim from a persistently-holding Steam client (investigate, #9):** the
  latch-heal rebind only fires when a hidraw holder *closes* its fd; the idle Steam client
  never does, so a firmware "light out" latch it set may not be reclaimed without a pad
  power-cycle. Not a confirmed standing bug - normal operation wins against a running Steam;
  the dark state was seen only under abnormal churn. Needs a clean-room reproduction before
  any fix (candidate: rate-limited rearm gated to the idle-client case; cost: input hitch).
  See GitHub Issue #9.
- **Remapper liveness on sub-poll blips (resolved 2026-07-05):** a BT blip shorter than one
  2 s poll can kill the remapper's grab without the pad appearing absent. The reconcile now
  checks `remap_healthy()` on every present pad and restarts the remapper when the mode wants
  one but the thread is dead (`tests/test_reconcile.py`, Scenario H) - kept here for the record.
- **Mode switch blocks the D-Bus main loop (deferred):** `set_mode` runs in the GLib main
  thread and calls `apply_mode`, which shells out to the gate helper (`sudo`, up to a 10 s
  timeout) and can `sleep` up to 3 s in `_refresh_nodes` - all while holding the manager
  lock. A tray click can therefore freeze the tray/menu for ~1-2 s (worst case longer) and
  stall the monitor thread. A correct fix moves the privileged, blocking work off the main
  thread (worker/queue) with per-instance locking so the reconcile can't race the same pad.
  Deferred deliberately: it is an invasive concurrency change with real regression risk and
  needs hardware to verify, and the visible cost on a single-user desktop tray is small.
- **Phantom pad in a dual `ps5-xbox` remap (deferred, 2026-07-15):** Steam lists a fifth,
  inert PlayStation pad once the second DualSense is emulated - its physical evdev node,
  re-opened by Steam during the second transition's driver rebind. `EVIOCGRAB` keeps it
  input-dead (no double input), so it is cosmetic. No lightweight robust fix exists (the
  daemon shares Steam's UID, so an evdev permission barrier locks out the remapper too); the
  robust fix is a root fd-passing grab with a born-gated evdev node. Deferred pending a
  game-side slot test - escalate only on functional harm. Full write-up:
  `docs/decisions/evdev-enumeration-leak.md`.
