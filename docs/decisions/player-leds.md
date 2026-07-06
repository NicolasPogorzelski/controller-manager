# Decision: Daemon-Owned Player Numbers

## Context

Three parties write the DualSense white player LEDs: the kernel driver (per its own
player-id allocation, re-allocated on every driver rebind), Steam (its slot count, raw
via hidraw, rewritten on every (re)enumeration of any pad), and this daemon. Gate
transitions rebind the driver by design, so both foreign numbering schemes drift under
normal operation — field-observed as player LEDs rotating across pads whenever modes
were switched.

## Decision

The daemon owns the number. Each pad is assigned a player number at adoption (lowest
free — i.e. overall connection order), persisted per stable ident in
`controller-modes.json` under the `_players` key so it survives reconnects AND daemon
restarts: restart re-adoption follows evdev node order, which earlier rebinds shuffle,
so the session order alone would swap numbers between restarts.

Asserted through the kernel LED class (`controller-led player <inputN> <1-4>`,
PS5-authentic patterns — the lit-LED count equals the player number) together with
every lightbar apply, plus a one-shot re-assert ~6 s after ANY pad's gate churn: that
is when Steam recounts and rewrites its slots on every pad it holds. The one-shot
deliberately leaves the lightbar alone — outside the settle window it may legitimately
belong to a game (see [steam-coexistence](steam-coexistence.md)).

The tray label shows the same number ("DualSense 1" = the pad with one lit LED), so
menu and hardware cannot disagree — a positional label would flip after a
drop-and-readopt while the LEDs kept their number.

## Consequences

- Numbers are stable across mode switches, reconnects and daemon restarts; the
  first-connected pad stays player 1 for good.
- A number is freed only by a full removal (grace elapsed) and reused by the next new
  pad — a fixed set of pads keeps fixed numbers forever.
- Steam's own count may still flash for a few seconds after churn until the re-assert
  wins; accepted, same policy as the lightbar reopen handling.
- Xbox pads get a number too (for stable labels), but their LED ring is left to the
  kernel — only the ps5 family is asserted.
