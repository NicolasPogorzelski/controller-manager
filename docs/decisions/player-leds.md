# Decision: Daemon-Owned Player Numbers

## Context

Three parties write the DualSense white player LEDs: the kernel driver (per its own
player-id allocation, re-allocated on every driver rebind), Steam (its slot count, raw
via hidraw, rewritten on every (re)enumeration of any pad), and this daemon. Gate
transitions rebind the driver by design, so both foreign numbering schemes drift under
normal operation - field-observed as player LEDs rotating across pads whenever modes
were switched.

## Decision

The daemon owns the number. Each pad is assigned a player number at adoption (lowest
free - i.e. overall connection order), persisted per stable ident in
`controller-modes.json` under the `_players` key so it survives reconnects AND daemon
restarts: restart re-adoption follows evdev node order, which earlier rebinds shuffle,
so the session order alone would swap numbers between restarts.

Asserted through the kernel LED class (`controller-led player <inputN> <1-4>`,
PS5-authentic patterns - the lit-LED count equals the player number) together with
every lightbar apply, plus a one-shot re-assert ~6 s after ANY pad's gate churn: that
is when Steam recounts and rewrites its slots on every pad it holds. The one-shot
deliberately leaves the lightbar alone - it may legitimately belong to a running
game at that moment (see [steam-coexistence](steam-coexistence.md)).

The tray label shows the same number ("DualSense 1" = the pad with one lit LED), so
menu and hardware cannot disagree - a positional label would flip after a
drop-and-readopt while the LEDs kept their number.

When a pad stays off long enough to be dropped, its number leaves a hole in the
connected set (e.g. 1, 3, 4). The daemon compacts that hole - renumbering the
remaining pads to a contiguous 1..N, **order-preserving** so no two continuously
connected pads swap and nobody overtakes anybody - but only after `COMPACT_GRACE`
(5 min), far longer than the removal grace. That window is deliberately sized for a
dead battery/accu swap: a pad that returns within it reclaims its old number (still
free) and no renumber happens; only a pad that stays off past it gives up its slot.
A tray entry, "Renumber players" (shown only while a hole exists), forces the
compaction immediately. The absent pad's stale reservation is left in `_players`
untouched - a much-later return finds its old number now worn by a lower-numbered
peer and takes the next free one, so a reclaim never reopens a closed hole.

## Consequences

- Numbers are stable across mode switches, reconnects and daemon restarts; a pad
  keeps its number as long as the connected set is unchanged.
- A number is freed by a full removal (grace elapsed); the resulting hole is then
  compacted after `COMPACT_GRACE`, or at once via the tray entry, so the connected
  pads stay a contiguous 1..N. A brief power-off (battery swap) inside the window
  reclaims the number and triggers no renumber.
- Steam's own count may still flash for a few seconds after churn until the re-assert
  wins; accepted, same policy as the lightbar reopen handling.
- Xbox pads get a number too (for stable labels), but their LED ring is left to the
  kernel - only the ps5 family is asserted.
