# Decision: Output Protocol Constraints

## Context

The remapper can, in principle, emit a virtual controller of either type. In practice the
two directions are not symmetric, because of how applications discover and read each type.

## What works: PlayStation -> Xbox

Emitting a virtual **Xbox** pad works reliably. Xbox-style controllers are consumed
through the standard evdev / XInput path, which reads the virtual device's events directly.
This is the project's primary purpose: making a PlayStation controller usable in
applications that only speak the Xbox (XInput) protocol.

Verified at the event level: with a PlayStation pad in "output as Xbox" mode, the virtual
device emits standard Xbox button codes (`BTN_SOUTH`/`EAST`/`NORTH`/`WEST` and the
shoulder buttons), and a simultaneously-native second controller continues to emit its own
events independently.

## What does not work: Xbox -> PlayStation

Emitting a virtual **PlayStation** pad is **not offered**, even though the evdev
translation itself is correct.

Applications commonly recognise a PlayStation controller by its vendor/product id and then
read it through a **HID API that opens `/dev/hidraw*` directly**. A virtual `uinput`
device has **no hidraw node**. So when the virtual pad claims a PlayStation identity, the
application's HID backend finds nothing to open and falls back to a generic evdev joystick
backend - which carries axes but not a full button mapping. The result in real
applications is sticks and the d-pad working while face buttons do not. Unusable.

This is the mirror image of [the hidraw gate problem](hidraw-gate.md): there, a physical
device's *extra* hidraw node was a leak; here, a virtual device's *missing* hidraw node is
the defect.

## Decision

- Offer **PlayStation -> Xbox** and the two native modes.
- Do **not** offer Xbox -> PlayStation in the tray. A persisted value for the removed mode
  falls back to the Xbox native default.
- **Keep the translation code** (the virtual PlayStation target and the button quirk
  table) in the source, behind the unused mode, so a future fix can re-enable it.

## Rejected workaround

Forcing the HID backend off for PlayStation devices (an SDL-style toggle) would route all
PlayStation pads through the evdev backend and could make the virtual pad's buttons appear
- but it would also degrade a **real** PlayStation controller in native mode (losing
features that depend on the HID path). It is therefore not used.

## Possible future direction

A correct fix would emit a virtual **HID** device (e.g. via `uhid`) that presents a full
controller HID descriptor and input reports, so the application's HID backend has a node to
open. That is a substantially larger effort and is not currently planned.
