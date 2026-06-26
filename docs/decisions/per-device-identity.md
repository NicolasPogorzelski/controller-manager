# Decision: Per-Device Identity

## Context

Two identical controllers (same vendor and product id) must be configurable
independently — for example, one native and one remapped at the same time. A product-ID
key cannot tell them apart, and the `/dev/input/eventX` path is not stable across
reconnects, so it cannot key persisted configuration.

## Decision

Identify each controller by its **stable per-device id** — the evdev `uniq` value, which
is the Bluetooth MAC (or serial) for the controllers this tool targets.

- **Configuration** in `controller-modes.json` is keyed by `uniq`.
- The **tray label** is disambiguated with a short tail of the `uniq`, so two identical
  controllers are distinguishable in the menu.
- The runtime device map is still keyed by the current `eventX` path (it must be, for
  hotplug), but the *identity* that survives reconnects and that the user configures is the
  `uniq`.

If a device exposes no `uniq`, the daemon falls back to a `vendor:product` key for that
device.

## Consequences

- Two identical controllers keep separate, persistent modes.
- A controller keeps its mode across reconnects and across `eventX` renumbering.
- Because the [hidraw gate](hidraw-gate.md) also operates per node, the whole pipeline —
  detection, configuration, remapping, and gating — is independent per physical device.

## Migration note

An earlier version keyed configuration by `vendor:product`. Such a key is dead once a
device with a real `uniq` is present (the device matches on its `uniq` instead). Stale
`vendor:product` entries can be removed from `controller-modes.json` without effect.
