# Runbook: Verify a Remap

Confirm that a remap is structurally correct — without launching any application — by
checking four things: the source is grabbed, a virtual device exists, the source's hidraw
node is gated, and a second controller is unaffected.

## Preconditions

- The service is running (`systemctl --user is-active controller-manager.service`).
- At least one supported controller connected. For the independence check, two.
- `lsof` available.

## Steps

### 1. Identify the controller's nodes

For each connected controller, find its evdev node, stable id, and hidraw node:

```bash
python3 - <<'PY'
import evdev, os, glob
from evdev import ecodes as e
FAM = {0x054c: "playstation", 0x045e: "xbox"}
def hidraw(path):
    base = f"/sys/class/input/{os.path.basename(path)}/device"
    if not os.path.exists(base): return None
    hid = os.path.realpath(os.path.join(base, "..", ".."))
    n = glob.glob(os.path.join(hid, "hidraw", "hidraw*"))
    return f"/dev/{os.path.basename(n[0])}" if n else None
for p in sorted(evdev.list_devices()):
    d = evdev.InputDevice(p)
    if d.info.vendor not in FAM: continue
    keys = d.capabilities().get(e.EV_KEY, [])
    if e.BTN_GAMEPAD not in keys and e.BTN_SOUTH not in keys: continue
    print(f"{p}  {d.info.vendor:04x}:{d.info.product:04x}  uniq={d.uniq or '-'}  hidraw={hidraw(p)}  {d.name!r}")
PY
```

### 2. Set a mode

In the tray, set one controller to a remap mode (e.g. **Output as Xbox**). Leave a second
controller native if you have one.

## Verification

**Source is grabbed** — only the daemon holds the remapped controller's evdev node; a
native controller's node is free:

```bash
lsof /dev/input/eventX     # remapped → held by python3 (the daemon)
lsof /dev/input/eventY     # native   → no holder
```

**Virtual device exists** — a virtual target controller appears for the remapped source:

```bash
python3 -c "import evdev; print([evdev.InputDevice(p).name for p in evdev.list_devices()])"
# remapped source → a 'Microsoft X-Box 360 pad' entry is present
```

**hidraw is gated per node** — the remapped controller's node is closed, the native one
open:

```bash
ls -l /dev/hidrawX     # remapped → c--------- (000)
ls -l /dev/hidrawY     # native   → crw-rw-rw- (666)
```

**Cleanup is correct** — set both controllers back to native and re-check: no virtual
device remains, and both hidraw nodes return to `666`.

**(Optional) Functional check** — read the virtual device while pressing buttons on the
remapped controller to confirm events are translated:

```bash
python3 -c "import evdev; d=evdev.InputDevice('/dev/input/eventZ');  # the virtual node
print('reading 10s...');
import time; end=time.time()+10
for ev in d.read_loop():
    if ev.type==evdev.ecodes.EV_KEY and ev.value==1: print(evdev.ecodes.keys.get(ev.code))
    if time.time()>end: break"
```

## Failure modes

| Observation | Likely cause |
|---|---|
| Remapped node still has other holders in `lsof` | grab failed — check the daemon log for `grab failed` |
| No virtual device appears | `uinput` not writable, or grab failed and the virtual device was cleaned up |
| hidraw still `666` while remapped | gate helper missing or `sudo -n` denied — see [the hidraw gate](../docs/decisions/hidraw-gate.md) |
| Virtual device lingers after returning to native | lifecycle bug — restart the service and report it |

## Rollback

Set every controller back to native in the tray, or restart the service to restore all
nodes:

```bash
systemctl --user restart controller-manager.service
```
