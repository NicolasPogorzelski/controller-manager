# Decision: Per-Device Identity

## Context

Two identical controllers (same vendor and product id) must be configurable
independently - for example, one native and one remapped at the same time. A product-ID
key cannot tell them apart, and the `/dev/input/eventX` path is not stable across
reconnects, so it cannot key persisted configuration.

## Decision

Identify each controller by a **stable per-device id** - the evdev `uniq` value (the
Bluetooth MAC / serial) when the pad reports one, else its physical attachment point
(`phys:...`), else `vendor:product` as a last resort. This is what `ident_of()` computes.

- **Configuration** in `controller-modes.json` is keyed by this identity.
- The **tray label** is disambiguated by the pad's daemon-assigned **player number** (the
  same one shown on the DualSense white player LEDs), so two identical controllers are
  distinguishable in the menu and menu and hardware always agree - see
  [player-leds](player-leds.md).
- The runtime device map is still keyed by the current `eventX` path (it must be, for
  hotplug), but the *identity* that survives reconnects and that the user configures is the
  stable id above.

The fallback chain matters for two identical serial-less USB pads: they report no `uniq`,
so `phys:` (the USB port) is what keeps them apart; `vendor:product` alone would collapse
them into one.

## Consequences

- Two identical controllers keep separate, persistent modes.
- A controller keeps its mode across reconnects and across `eventX` renumbering.
- Because the [hidraw gate](hidraw-gate.md) is also keyed by the pad's stable identity
  (its `HID_UNIQ` marker), the whole pipeline - detection, configuration, remapping, and
  gating - is independent per physical device.

## Migration note

An earlier version keyed configuration by `vendor:product`. Such a key is dead once a
device with a real `uniq` is present (the device matches on its `uniq` instead). Stale
`vendor:product` entries can be removed from `controller-modes.json` without effect.
