#!/usr/bin/env bash
# Build the HID-BPF report-descriptor fixup(s) in this directory.
#
# HID-BPF objects are compiled against the running kernel's type information
# (BTF), so we generate vmlinux.h fresh from /sys/kernel/btf/vmlinux at build
# time rather than committing a large generated header or a prebuilt binary.
# The result is written to ./build/<name>.bpf.o and its path printed on stdout.
#
# Requires: clang, bpftool, libbpf headers (bpf/bpf_helpers.h), and a kernel
# built with CONFIG_HID_BPF + BTF. Run standalone to rebuild, or via install.sh.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

src="0010-Microsoft__Xbox-One-S-02FD.bpf.c"
out_dir="build"
out="$out_dir/${src%.bpf.c}.bpf.o"

# ── Preflight ────────────────────────────────────────────────────────────────
missing=()
command -v clang   >/dev/null || missing+=("clang")
command -v bpftool >/dev/null || missing+=("bpftool")
[ -e /usr/include/bpf/bpf_helpers.h ] || missing+=("libbpf-devel (bpf/bpf_helpers.h)")
if [ "${#missing[@]}" -gt 0 ]; then
    echo "hid-bpf/build.sh: missing build prerequisites: ${missing[*]}" >&2
    echo "  Fedora:  sudo dnf install clang bpftool libbpf-devel" >&2
    exit 1
fi
if [ ! -r /sys/kernel/btf/vmlinux ]; then
    echo "hid-bpf/build.sh: /sys/kernel/btf/vmlinux not found - kernel lacks BTF" >&2
    echo "  (CONFIG_DEBUG_INFO_BTF); HID-BPF cannot be built on this kernel." >&2
    exit 1
fi

# ── Build ────────────────────────────────────────────────────────────────────
mkdir -p "$out_dir"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

bpftool btf dump file /sys/kernel/btf/vmlinux format c > "$tmp/vmlinux.h"

# -Wno-missing-declarations silences benign warnings from the generated
# vmlinux.h on recent kernels; they do not affect the object.
clang -std=gnu11 -fno-stack-protector -O2 -g -target bpf \
      -D__TARGET_ARCH_x86 -Wno-missing-declarations \
      -I"$tmp" -I./include \
      -c "$src" -o "$tmp/unstripped.o"

# 'bpftool gen object' finalises/strips the object the way udev-hid-bpf expects.
bpftool gen object "$out" "$tmp/unstripped.o"

echo "$out"
