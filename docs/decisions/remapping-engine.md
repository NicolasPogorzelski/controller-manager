# Decision: Remapping Engine - evdev Grab + uinput

## Context

To make a controller appear as a different type, the system must intercept the physical
device's events and re-emit them under a new identity, before applications enumerate
input devices. This has to work for any launcher and any application, without per-title
configuration.

## Decision

Use the Linux input subsystem directly:

- **`EVIOCGRAB`** on the source `evdev` node for exclusive access, so no other consumer
  sees the physical device while it is remapped.
- A **`uinput`** virtual device created with the target identity (vendor/product/version)
  and the source's capabilities, into which the source's events are forwarded.

This operates below the application layer, so the result is uniform across launchers and
needs no game- or app-specific settings.

## Consequences and the pitfalls that shaped the design

### The virtual device is created before the grab

`uinput` creation happens first, then the grab. If the grab fails (another process holds
the device), the virtual device is explicitly closed. Skipping that cleanup would leave an
orphaned virtual controller with no remapper behind it.

### Virtual devices must be excluded from detection

The detection scan filters out the daemon's own virtual devices by name and by path.
Without this, a freshly created virtual pad would be detected as a new physical controller
and remapped again - an endless loop.

### Capabilities are advertised in translated form

Some controllers expose a non-standard evdev button layout. Forwarding raw codes would
make the consuming side map the *target* identity against the *source's* layout, landing
buttons in the wrong place. The remapper holds a per-source quirk table and advertises the
translated (standard) codes on the virtual device, then translates each event as it is
forwarded.

### Codes are also translated per *target* identity

Even a fully standard source cannot always be passed through 1:1, because the kernel
aliases positional and lettered button constants (`BTN_X == BTN_NORTH == 0x133`,
`BTN_Y == BTN_WEST == 0x134`) and driver families assign them inverted: `hid-playstation`
emits Triangle (top) as `0x133` and Square (left) as `0x134`, while `xpad` emits X (left)
as `0x133` and Y (top) as `0x134`. Consumers (SDL, Steam) resolve codes against the
device's *advertised* identity - so a DualSense passed through onto an "X-Box 360 pad"
had X and Y swapped in games (field-observed in Dave the Diver; every other code happens
to agree between the two drivers). A second, per-target table therefore swaps
`0x133 <-> 0x134` when the target is the virtual Xbox pad; it is composed with the
per-source quirk table (quirk first: source -> standard, then target: standard -> target).
A future `xbox-ps5` mode would need the inverse entry for the virtual DualSense target.

### Analog reports are bounded and keep only the latest value

Bluetooth DualSense devices can report hundreds of stick samples per second. Forwarding
every sample creates a downstream queue in Wine/SDL games, so a game may continue applying
old steering values after the physical stick has returned to center. DualSense sticks also
use unsigned `0..255` values, while an Xbox target expects signed values centered on zero.

For the virtual Xbox target, the remapper therefore:

- translates stick values to `-32768..32767`;
- snaps source values `120..135` to zero to absorb center jitter;
- collects the latest stick and trigger values from each source `SYN_REPORT` frame;
- emits at most one analog report every 1/60 second; and
- flushes pending analog values immediately when a digital or relative event must be sent.

Triggers are part of the same policy because a half-held L2/R2 value jitters continuously.
Treating trigger updates as immediate would flush pending sticks at the raw Bluetooth report
rate and recreate the same backlog. Fully pressed triggers often appear unaffected because
their value settles at the endpoint and stops generating updates.

Buttons, D-pad axes, and relative events are not rate-limited. The maximum added latency for
an analog transition is one 60 Hz interval (about 16.7 ms), while intermediate stale values
are replaced rather than queued.

### The event loop must be interruptible while idle

`evdev`'s blocking read waits for the next event. The first implementation tried to stop a
remapper by setting a flag and **closing the source fd from another thread**, expecting
the blocked read to wake. On Linux this is unreliable: closing a file descriptor does not
deterministically interrupt a `read()` blocked on it in another thread. When the
controller was idle (no events arriving), the remapper thread stayed blocked, kept its
`uinput` device open, and leaked an orphaned virtual controller - intermittently,
depending on whether a stray event happened to arrive.

The loop now uses `selectors` over **two** descriptors: the source fd and a **self-pipe**.
`stop()` writes one byte to the pipe; `select()` returns immediately and the loop exits and
cleans up (release grab, close source, close virtual device, close pipe). This wakes the
loop deterministically regardless of controller activity.

## Alternatives considered

- **Per-application environment toggles** - fragile, must be repeated for every launcher,
  and several do not reliably propagate environment variables to the application.
- **Userspace remappers that translate to keyboard/mouse** - wrong layer; they fight with
  controller-aware applications and produce mixed input.
