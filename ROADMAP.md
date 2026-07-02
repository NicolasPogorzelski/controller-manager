# Roadmap

Short, rolling list of where this project stands and what comes next. Newest status on top.

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
