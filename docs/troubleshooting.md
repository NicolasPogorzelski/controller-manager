# Troubleshooting

Quick checks first:

```bash
systemctl --user status controller-manager.service
journalctl --user -u controller-manager.service -f
```

## The tray icon does not appear

The tray uses the **StatusNotifierItem** specification. It requires a tray host that
implements it; some desktops need an extension to show such items. Confirm the daemon is
running (above) and that your desktop has a working StatusNotifierItem tray.

Note for developers: the item is registered with the `StatusNotifierWatcher` using its
**object path**, not a bus name. Registering with a bus name fails silently and the icon
never appears.

## `Permission denied` on `/dev/uinput`

The daemon needs write access to `/dev/uinput` to create virtual devices. On most
distributions this is granted by group membership. Verify access:

```bash
ls -l /dev/uinput
```

If your user lacks access, add the appropriate group (distribution-dependent) and
re-log in.

## Double input / wrong glyphs in a remapped mode

The physical controller is still reaching the application through its raw HID node — the
[hidraw gate](decisions/hidraw-gate.md) is not in effect. Check:

1. **Is the gate helper installed?** Without it, gating is a silent no-op.

   ```bash
   ls -l /usr/local/bin/controller-hidraw-gate /etc/sudoers.d/controller-hidraw
   ```

2. **Was the application started before the remap?** A process that already holds the
   hidraw fd keeps it across the later `chmod`. Set the mode first, then start the
   application — or restart the application.

Confirm the gate state directly (replace the node with your controller's, see the
[verification runbook](../runbooks/verify-remapping.md)):

```bash
ls -l /dev/hidrawN     # remapped → c--------- (000) ; native → crw-rw-rw- (666)
```

## The DualSense lightbar shows the wrong colour after a reconnect

Symptom: the pad is correctly remapped (e.g. output as Xbox, raw HID gated) but the lightbar
stays **blue** (the firmware default) instead of the mode colour — most visibly after
toggling the controller off/on several times. The mode itself is fine; only the colour cue
is stale, which can make a mode switch *look* like it did nothing.

Cause: on a reconnect the hid-playstation `…:rgb:indicator` LED node is renumbered (its name
follows the `inputN` counter, which bumps even when the `/dev/input/eventX` path is reused),
and the controller firmware resets the lightbar to its default on power-cycle. The daemon
re-resolves the live node and re-asserts the mode colour on the next monitor tick, so it
normally corrects itself within ~2 s. Confirm the kernel LED matches the mode:

```bash
# find the DualSense indicator LED, then read its stored colour
for l in /sys/class/leds/*:rgb:indicator; do
  echo "$l -> $(cat "$l/multi_intensity")  (brightness $(cat "$l/brightness"))"
done
# ps5-native = 0 0 255 (blue) ; ps5-xbox = 0 128 0 (green)
```

If `multi_intensity` already matches the mode but the **hardware** lightbar does not, the
driver dropped the output report on the wire (rare, Bluetooth). Re-selecting the mode in the
tray, or a reconnect, re-asserts it. If the LED value itself is wrong, check that
`controller-led` is installed and authorised (`/etc/sudoers.d/controller-hidraw`) — without
it the colour write is a silent no-op.

## A remapped controller has working sticks but dead buttons

This is the Xbox→PlayStation direction, which is intentionally **not offered** for exactly
this reason — see [output protocol constraints](decisions/output-protocol-constraints.md).
The supported directions are PlayStation→Xbox and the two native modes.

## Buttons are mapped to the wrong positions

Some controllers expose a non-standard evdev button layout. The remapper carries a
per-source quirk table that translates such layouts to standard codes. A new controller
model with an unusual layout may need an entry added to that table.

## Another remapper is fighting for the device

If a separate userspace remapper grabs controllers system-wide (translating them into a
virtual device of its own, or into keyboard/mouse events), applications will see mixed or
duplicated input. Disable other system-wide controller remappers so this daemon has sole
control of the physical devices.

## An orphaned virtual controller lingers

A correctly running daemon removes its virtual device as soon as a controller returns to
native mode. If you find a stale virtual pad, restart the service — it clears any leftover
state:

```bash
systemctl --user restart controller-manager.service
```

If you can reproduce a lingering virtual device during normal mode switching, that is a
bug worth reporting; see [the remapping engine decision](decisions/remapping-engine.md)
for the lifecycle that should prevent it.
