# Decision: dbusmenu Item Model — never recycle ids

## Context

The tray menu is served over `com.canonical.dbusmenu`. The original implementation
numbered items `1..n` on every (re)build. That recycles ids across **structural**
changes: with no controller connected the layout is `[1: "No controller connected",
2: separator, Quit]`; after a pad connects, id 2 becomes a *radio item*.

The GNOME appindicator host caches item properties **per id** and does not refresh them
when a `LayoutUpdated` reuses an id for a structurally different item. The result, verified
live against the daemon: the daemon serves `"DualSense mode" enabled=true` over D-Bus
while the tray renders the same item disabled — stuck with the stale separator/disabled
properties from the previous structure. Every disconnect/reconnect (menu shrinks to the
empty state and grows back) re-triggers it, which is why mode switching "died" after
Bluetooth churn.

A second, latent defect of rebuilding per call: the id→item mapping was derived live in
every D-Bus method, so the instance list could change between a host's `GetLayout` and
its click `Event` — the ids shift and the click lands on the wrong controller.

## Decision

1. **One cached model per revision.** The model (ordered items + id→action lookup) is
   built once per state change; `GetLayout`, `GetGroupProperties`, `GetProperty` and
   `Event` all answer from that snapshot. A click with an id from an older structure
   finds no lookup entry and is ignored — never misrouted.
2. **Fresh ids on every structural change.** Ids come from a monotonic counter and are
   never reused for a different item, so a host *must* treat them as new items and fetch
   fresh properties. `Quit` is the one exception: its kind, label and properties are
   constant forever, so its well-known id is safe.
3. **Display-only changes keep their ids** (radio checkmark, relabels) and are pushed as
   `ItemsPropertiesUpdated` deltas — the update path every SNI host handles reliably.
   `LayoutUpdated` (with a revision bump) is emitted only for structural changes.

Structure vs. display is decided by a signature over the item kinds and click targets
(`(radio, ident, mode)…`); labels and toggle-state are excluded from it deliberately.

## Consequences

- Menu entries can no longer get stuck disabled after connect/disconnect churn.
- Clicks are routed against exactly the model the host displayed.
- Ids grow monotonically (a handful per reconnect). The counter skips the reserved Quit
  id; overflow is not a practical concern within a session.
- Hosts that garbage-collect items on layout changes see strictly more conservative
  behaviour than before; hosts that cache per id are forced correct.

Regression-tested in `tests/test_menu_ids.py` (no bus required).
