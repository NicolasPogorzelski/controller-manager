// SPDX-License-Identifier: GPL-2.0-only
/* Copyright (c) 2026 controller-manager contributors
 *
 * HID report-descriptor fixup for the Bluetooth Xbox Wireless Controller
 * reporting product id 0x02FD (an "Xbox One S" era pad on newer firmware).
 *
 * This unit's firmware advertises a 306-byte HID report descriptor that is
 * truncated mid-item inside the force-feedback output collection: it opens
 * five COLLECTIONs but closes only three, so two collections are left open
 * and the descriptor ends on a dangling USAGE / LOGICAL_MINIMUM with no Main
 * item. hid_open_report() rejects such a descriptor outright, so NEITHER
 * hid-generic NOR xpadneo can bind and no input node is ever created:
 *
 *   playstation/xpadneo 0005:045E:02FD.*: unbalanced collection at end of
 *       report description
 *   ... probe with driver <x> failed with error -22
 *
 * A sibling pad reporting 0x02E0 ships a well-formed 306-byte descriptor that
 * ends in "... 81 02 c0" (INPUT, END_COLLECTION) and binds cleanly. The only
 * structural difference is the two missing END_COLLECTION bytes. Appending
 * them here yields exactly the balanced structure the working pad already has;
 * the sole content lost is the tail of one force-feedback OUTPUT field that
 * the firmware truncated anyway. All input fields (buttons, sticks, D-pad)
 * live in the well-formed leading section and are untouched.
 *
 * Microsoft fixed this in a later firmware, but that update is only reachable
 * via the Xbox Accessories app (Windows) or an Xbox console. This fixup makes
 * the pad usable on Linux without that. It is bound tightly to the exact
 * broken descriptor (size == 306 and the specific truncated tail), so a pad on
 * fixed firmware — which advertises a different descriptor — is left alone.
 *
 * A note on the product ids we match. The pad reports product 0x02FD, but
 * xpadneo rewrites it to 0x028E ("pretending XB1S Windows wireless mode")
 * during its probe — and that probe runs and rewrites the id BEFORE this
 * program is attached, whether or not it ultimately fails. So by the time the
 * udev rule fires and HID-BPF matches the device, the product is already
 * 0x028E. We list both: 0x028E is what actually matches when xpadneo is
 * present (the normal case), and 0x02FD covers a system without xpadneo where
 * the id is never rewritten. BUS_BLUETOOTH keeps this away from the identical
 * 0x028E id used by wired Xbox 360 pads (USB) and by our own virtual pad.
 *
 * We deliberately do NOT gate in probe(): for this device the parse failed, so
 * the report descriptor exposed to the probe syscall is empty (rdesc_size 0).
 * The real, raw 306-byte descriptor is only visible inside the rdesc fixup, so
 * that is where the size + tail guard lives.
 */

#include "vmlinux.h"
#include "hid_bpf.h"
#include "hid_bpf_helpers.h"
#include <bpf/bpf_tracing.h>

#define VID_MICROSOFT		0x045e
#define PID_XBOX_RAW		0x02fd	/* as the firmware reports it */
#define PID_XBOX_XPADNEO	0x028e	/* what xpadneo rewrites it to */

HID_BPF_CONFIG(
	HID_DEVICE(BUS_BLUETOOTH, HID_GROUP_GENERIC, VID_MICROSOFT, PID_XBOX_XPADNEO),
	HID_DEVICE(BUS_BLUETOOTH, HID_GROUP_GENERIC, VID_MICROSOFT, PID_XBOX_RAW)
);

/* Size of the broken descriptor as advertised by the pad. */
#define BROKEN_RDESC_SIZE	306

/*
 * Last 8 bytes of the broken descriptor. These are the dangling global/local
 * items that trail off where the two END_COLLECTION bytes should be:
 *   65 00   Unit (0)
 *   55 00   Unit Exponent (0)
 *   09 7c   Usage (0x7c)
 *   15 00   Logical Minimum (0)
 * Matching this exact tail keeps the fixup from touching any other descriptor.
 */
static const __u8 broken_tail[] = {
	0x65, 0x00, 0x55, 0x00, 0x09, 0x7c, 0x15, 0x00,
};

#define TAIL_OFFSET	(BROKEN_RDESC_SIZE - sizeof(broken_tail))

SEC(HID_BPF_RDESC_FIXUP)
int BPF_PROG(hid_fix_rdesc, struct hid_bpf_ctx *hctx)
{
	__u8 *data = hid_bpf_get_data(hctx, 0 /* offset */, 4096 /* size */);

	if (!data)
		return 0; /* EPERM check */

	/* Only the exact broken descriptor; leave anything else as-is. */
	if (hctx->size != BROKEN_RDESC_SIZE)
		return 0;

	if (__builtin_memcmp(data + TAIL_OFFSET, broken_tail, sizeof(broken_tail)))
		return 0;

	/* Close the two collections the firmware left open. */
	data[BROKEN_RDESC_SIZE + 0] = 0xc0; /* End Collection */
	data[BROKEN_RDESC_SIZE + 1] = 0xc0; /* End Collection */

	/* Positive return value = new descriptor size. */
	return BROKEN_RDESC_SIZE + 2;
}

HID_BPF_OPS(xbox_one_s_02fd) = {
	.hid_rdesc_fixup = (void *)hid_fix_rdesc,
};

SEC("syscall")
int probe(struct hid_bpf_probe_args *ctx)
{
	/*
	 * Attach whenever the device id matches. We cannot gate on the
	 * descriptor here: the failed parse leaves the probe's report
	 * descriptor empty (rdesc_size 0). The actual guard (exact size + tail)
	 * lives in hid_fix_rdesc, which is the only place the raw descriptor is
	 * visible; on any non-matching descriptor it is a no-op.
	 */
	ctx->retval = 0;
	return 0;
}

char _license[] SEC("license") = "GPL";
