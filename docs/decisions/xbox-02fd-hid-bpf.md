# Decision: HID-BPF fixup for the Bluetooth Xbox pad (product 0x02FD)

## Context

A Bluetooth Xbox Wireless Controller reporting USB product id `0x02FD` connects and pairs
normally — BlueZ shows it `Connected`, with the HID service — but it produces **no input
device at all**: no `/dev/input/event*`, no `js*`, no `hidraw*`. It is therefore invisible
to everything downstream: the tray never lists it, and no application (RetroArch, any SDL
title) can see it. Two structurally identical DualSense pads on the same host work fine, as
does a *different* Xbox pad reporting product `0x02E0`.

The cause is below this project entirely, in the kernel HID layer:

```
playstation/xpadneo 0005:045E:02FD.*: unbalanced collection at end of report description
... probe with driver <x> failed with error -22
```

The pad's firmware advertises a **306-byte HID report descriptor that is truncated
mid-item** inside the force-feedback output collection. It opens five `COLLECTION`s but
closes only three; the last bytes are a dangling `USAGE` / `LOGICAL_MINIMUM` with no Main
item and no closing `END_COLLECTION`. `hid_open_report()` rejects such a descriptor
outright, so **no driver can bind** — not `hid-generic`, not `xpadneo`. No bind means no
input node.

The working `0x02E0` sibling ships a well-formed 306-byte descriptor that ends in
`… 81 02 c0` (INPUT, END_COLLECTION) and binds cleanly. The only structural difference is
the two missing `END_COLLECTION` bytes. This is a per-firmware bug in the `0x02FD` unit;
Microsoft fixed it in later firmware.

## Options considered

1. **Update the controller firmware** — the real fix, but it is only reachable via the Xbox
   Accessories app (Windows) or an Xbox console. Neither was available, and requiring either
   defeats the point of a Linux-side tool.
2. **Patch xpadneo** — its `report_fixup` already massages these pads, but it does not add
   the missing bytes, and it ships here as a packaged akmod. A downstream patch would have to
   be re-applied against every package update.
3. **HID-BPF report-descriptor fixup** *(chosen)* — attach a small BPF program at device
   probe that appends the two missing `END_COLLECTION` bytes, yielding exactly the balanced
   descriptor the working pad already has. The kernel supports it (`CONFIG_HID_BPF`, BTF
   present), Fedora packages the loader (`udev-hid-bpf`), and it needs no changes to the
   kernel or to xpadneo. There is direct precedent: udev-hid-bpf already ships an equivalent
   rdesc fixup for the Xbox Elite 2 (`0x0B22`) over Bluetooth.

## Decision

Ship `hid-bpf/0010-Microsoft__Xbox-One-S-02FD.bpf.c`, a `HID_BPF_RDESC_FIXUP` program built
and installed by `install.sh` via `udev-hid-bpf`. When the exact broken descriptor appears
(size 306 with the specific truncated tail `65 00 55 00 09 7c 15 00`), it writes two `0xC0`
bytes and returns the new size 308. The only content lost is the tail of one force-feedback
OUTPUT field the firmware had already truncated; every input field (buttons, sticks, D-pad)
lives in the well-formed leading section and is untouched. After the fixup the descriptor
parses, a driver binds, and the pad appears as an ordinary gamepad that `scan_controllers()`
adopts as the `xbox` family with no daemon changes.

Two implementation facts, learned the hard way and encoded in the source:

- **The device is matched as `0x028E`, not `0x02FD`.** xpadneo rewrites the product id
  `0x02FD → 0x028E` inside its probe (“pretending XB1S Windows wireless mode”), and that
  probe runs *before* the BPF program is attached, whether or not it ultimately fails. So by
  the time the udev rule fires the device already reports `0x028E`. The program's
  `HID_BPF_CONFIG` lists both ids (`0x028E` for the normal xpadneo case, `0x02FD` as a
  fallback for a host without xpadneo), all scoped to `BUS_BLUETOOTH` so the identical
  `0x028E` used by wired Xbox 360 pads (USB) and by this project's own virtual pad cannot
  match.

- **The guard lives in the rdesc fixup, not in `probe()`.** Because the initial parse
  failed, the report descriptor exposed to the `probe` syscall is empty (`rdesc_size` 0), so
  probe cannot gate on it. The raw 306-byte descriptor is only visible inside the fixup, so
  the size + tail check is there; `probe()` merely accepts the device-id match. On any other
  descriptor the fixup is a no-op, so attaching to unrelated `0x028E` Bluetooth devices is
  harmless.

With this pad, the driver that ultimately binds after the late BPF reprobe is `hid-generic`
(xpadneo's auto-load aliases key on `0x02FD`/`0x0B22`, not the rewritten `0x028E`, and it
does not re-grab on the reprobe). `hid-generic` exposes a complete, correctly-laid-out
gamepad — A/B/X/Y, both triggers, both sticks, hat D-pad — which is fully usable; xpadneo is
not required for this pad to work.

## Consequences

- **New build dependency, gated to opt-in.** Building the object needs `clang`, `bpftool`,
  `libbpf-devel`, `udev-hid-bpf`, and a BTF-enabled kernel. Because the fixup only matters to
  owners of the `0x02FD` pad, `install.sh` treats a missing prerequisite as a
  skip-with-warning — a DualSense-only install is never blocked by it.
- **Built against the running kernel.** `hid-bpf/build.sh` regenerates `vmlinux.h` from
  `/sys/kernel/btf/vmlinux` at install time rather than committing a large header or a
  prebuilt binary; `build/` is git-ignored.
- **Licence boundary.** The BPF program and the vendored `hid-bpf/include/*` headers are
  GPL-2.0-only (kernel-space code calling GPL BPF helpers), separate from the MIT userspace
  daemon. See `hid-bpf/README.md`.
- **Self-healing on reconnect.** The udev rule re-attaches the program on every `add`, so a
  Bluetooth reconnect comes back working with no manual step.
- **Narrow by construction.** A `0x02FD` pad on fixed firmware advertises a different
  descriptor, so the size + tail guard leaves it alone; nothing else on the system is
  affected.
