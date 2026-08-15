# hid-bpf - kernel-side report-descriptor fixups

This directory holds HID-BPF programs that patch a controller's HID report descriptor in
the kernel before it is parsed. They exist to make otherwise-unusable pads work; the
userspace daemon (`../controller-manager.py`) needs no knowledge of them.

## Contents

- `0010-Microsoft__Xbox-One-S-02FD.bpf.c` - appends the two missing `END_COLLECTION` bytes
  to the truncated HID descriptor of the Bluetooth Xbox pad reporting product `0x02FD`,
  which otherwise fails to parse and produces no input device at all. Full rationale in
  [../docs/decisions/xbox-02fd-hid-bpf.md](../docs/decisions/xbox-02fd-hid-bpf.md).
- `include/` - the minimal vendored HID-BPF headers (`hid_bpf.h`, `hid_bpf_helpers.h`) the
  program includes, copied verbatim from upstream `udev-hid-bpf`.
- `build.sh` - builds the program(s) against the running kernel's BTF into `build/`
  (git-ignored). `vmlinux.h` is generated at build time, not committed.

## Requirements

- A kernel with `CONFIG_HID_BPF=y` and BTF (`/sys/kernel/btf/vmlinux`).
- `clang`, `bpftool`, `libbpf-devel` (for `bpf/bpf_helpers.h`), and `udev-hid-bpf`.
  On Fedora: `sudo dnf install udev-hid-bpf clang bpftool libbpf-devel`.

## Build and install

`../install.sh` does this automatically (skipping with a warning if the toolchain is
absent). To do it by hand:

```sh
./build.sh                                              # -> build/<name>.bpf.o
sudo udev-hid-bpf install --force build/<name>.bpf.o    # -> /etc/udev-hid-bpf + udev rule
sudo udevadm control --reload
sudo udevadm trigger --action=add --subsystem-match=hid # attach to an already-connected pad
```

Inspect a built object with `udev-hid-bpf inspect build/<name>.bpf.o`. To remove a fixup,
delete its files under `/etc/udev-hid-bpf/` and `/etc/udev/rules.d/99-hid-bpf-*.rules` and
reload udev (the `udev-hid-bpf install` output prints the exact paths).

## Licence

Unlike the MIT-licensed userspace daemon, the code here is **GPL-2.0-only**: BPF programs
that call kernel BPF helpers must be GPL, and the vendored `include/*` headers are
GPL-2.0-only upstream. Each file carries its own SPDX identifier.
