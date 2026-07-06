#!/usr/bin/env python3
"""
Controller Manager — unified tray + per-controller remapping.

Supports:
  PlayStation DualSense  ->  native  |  output as Xbox 360
  Xbox controller        ->  native  (xbox->ps5 unusable in apps; see
                                       docs/decisions/output-protocol-constraints.md)
Multiple controllers simultaneously, each with its own mode.
"""

import os, sys, json, signal, threading, time, subprocess, glob, selectors
import evdev
from evdev import UInput, ecodes as e
import dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

# ── Constants ────────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.expanduser("~/.config/controller-modes.json")

# (vendor, product) → (display name, controller family)
# Recognise controllers by USB/BT vendor + gamepad capability rather than a
# fixed product-ID list (which keeps missing models — e.g. the BT Xbox pad
# 045e:02e0). Vendor → controller family.
VENDOR_FAMILY = {
    0x054c: "ps5",    # Sony
    0x045e: "xbox",   # Microsoft
}

# Optional nicer display names per (vendor, product); falls back to the kernel
# device name when unknown.
CONTROLLER_NAMES = {
    (0x054c, 0x0ce6): "DualSense",
    (0x054c, 0x0df2): "DualSense Edge",
    (0x045e, 0x028e): "Xbox 360",
    (0x045e, 0x02ea): "Xbox One",
    (0x045e, 0x02fd): "Xbox One S",
    (0x045e, 0x02e0): "Xbox One S (BT)",
    (0x045e, 0x0b12): "Xbox Series X/S",
    (0x045e, 0x0b13): "Xbox Series X/S (BT)",
    (0x045e, 0x0b20): "Xbox Series X/S",
}

DEFAULT_MODES = {
    "ps5":  "ps5-native",
    "xbox": "xbox-native",
}

# Lightbar colors per ps5 mode (R, G, B 0-255).
# Only DualSense-family controllers have a lightbar; Xbox is always skipped.
MODE_LED = {
    "ps5-native": (0,   0, 255),   # blue  — native PlayStation feel
    "ps5-xbox":   (0, 128,   0),   # green — signals active Xbox emulation
}

MODE_LABELS = {
    "ps5-native":  "DualSense mode",
    "ps5-xbox":    "Emulate Xbox",
    "xbox-native": "Xbox mode",
    "xbox-ps5":    "Emulate PS5",
}

# Modes offered in the tray menu per family. "xbox-ps5" is intentionally NOT
# listed: the evdev translation is correct, but applications read PlayStation
# VID:PIDs via a HID API (hidraw) and a virtual uinput pad has no hidraw node,
# so only axes (not buttons) reach applications — unusable in practice. See
# docs/decisions/output-protocol-constraints.md. The translation infrastructure
# (VIRTUAL_PS5 / QUIRK_BUTTON_MAP / _target_for_mode) is kept so a future
# uhid-based fix can re-enable it.
MODES_FOR_FAMILY = {
    "ps5":  ["ps5-native",  "ps5-xbox"],
    "xbox": ["xbox-native"],
}

VIRTUAL_XBOX = dict(
    name="Microsoft X-Box 360 pad",
    vendor=0x045e, product=0x028e, version=0x0114,
    bustype=e.BUS_USB,
)
VIRTUAL_PS5 = dict(
    name="Sony Interactive Entertainment DualSense Wireless Controller",
    vendor=0x054c, product=0x0ce6, version=0x8100,
    bustype=e.BUS_BLUETOOTH,
)

VIRTUAL_NAMES = {VIRTUAL_XBOX["name"], VIRTUAL_PS5["name"]}

# Some controllers expose a non-standard evdev button layout via their kernel
# driver; a raw passthrough then mismatches what SDL expects for the *target*
# identity (buttons land wrong / dead). Per source (vendor, product): source
# evdev code → standard gamepad code. Codes not listed pass through unchanged.
# Empirically mapped by position (see docs/decisions/remapping-engine.md).
QUIRK_BUTTON_MAP = {
    (0x045e, 0x02e0): {            # Xbox One S — old Bluetooth firmware
        e.BTN_C:    e.BTN_WEST,    # X
        e.BTN_WEST: e.BTN_TL,      # LB
        e.BTN_Z:    e.BTN_TR,      # RB
        e.BTN_TL:   e.BTN_SELECT,  # View
        e.BTN_TR:   e.BTN_START,   # Menu
        e.KEY_MENU: e.BTN_MODE,    # Xbox / Guide
        e.BTN_TL2:  e.BTN_THUMBL,  # L3
        e.BTN_TR2:  e.BTN_THUMBR,  # R3
        # A→BTN_A(=SOUTH), B→BTN_B(=EAST), Y→BTN_NORTH already standard.
    },
}

# The 0x133/0x134 codes are positionally inverted between driver families:
# hid-playstation emits Triangle (top) → BTN_NORTH (0x133) and Square (left)
# → BTN_WEST (0x134), while xpad assigns the SAME codes the other way round —
# X (left) → BTN_X (== BTN_NORTH) and Y (top) → BTN_Y (== BTN_WEST).
# Consumers resolve codes against the advertised identity, so a 1:1
# passthrough onto an "X-Box 360 pad" swaps X/Y in games. All other codes
# (A/B, Select/Start/Guide, sticks and triggers via ABS) happen to agree.
# Per target name: standard source code → code expected under that identity.
TARGET_BUTTON_MAP = {
    VIRTUAL_XBOX["name"]: {
        e.BTN_NORTH: e.BTN_WEST,   # Triangle → 0x134, read as Y (top)
        e.BTN_WEST:  e.BTN_NORTH,  # Square   → 0x133, read as X (left)
    },
}

def compose_button_maps(quirk, target):
    """Chain quirk (source code → standard code) and target (standard code →
    code under the target identity) into the single dict the Remapper applies
    per event; None when both are empty."""
    quirk, target = quirk or {}, target or {}
    combined = {src: target.get(std, std) for src, std in quirk.items()}
    for std, tgt in target.items():
        combined.setdefault(std, tgt)
    return combined or None

# DBus names
BUS_NAME  = "org.kde.StatusNotifierItem-ctrlmgr-1"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
QUIT_ID   = 9999


# ── Config ───────────────────────────────────────────────────────────────────

def load_config():
    try:
        return json.loads(open(CONFIG_FILE).read())
    except Exception:
        return {}

def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    open(CONFIG_FILE, "w").write(json.dumps(cfg, indent=2))


# ── hidraw gate ──────────────────────────────────────────────────────────────
#
# An evdev grab (EVIOCGRAB) only blocks the evdev node. Some applications
# read game controllers straight from /dev/hidraw*, so a remapped DualSense
# would still reach the application as a PlayStation pad — in parallel with our
# virtual Xbox pad — causing double input. We therefore chmod the physical
# controller's hidraw node to 000 for the duration of any remap. This is
# system-wide and launcher-agnostic: it hides the pad from every application
# alike. The chmod needs root, delegated to a tightly-scoped helper via a
# NOPASSWD sudoers rule. If the helper/rule is absent, this is a silent no-op.
#
# The gate operates on a SPECIFIC hidraw node (not a VID:PID), so two identical
# controllers (same VID:PID, different physical device) can be gated indepen-
# dently — only the remapped one is hidden.

GATE_BIN = "/usr/local/bin/controller-hidraw-gate"
LED_BIN  = "/usr/local/bin/controller-led"

def hidraw_for_event(ev_path):
    """Return all /dev/hidrawN nodes for the HID device behind an evdev node.
    Returns an empty list when the device exposes no hidraw (e.g. xpad)."""
    base = f"/sys/class/input/{os.path.basename(ev_path)}/device"
    if not os.path.exists(base):
        return []
    hid_dir = os.path.realpath(os.path.join(base, "..", ".."))
    nodes = glob.glob(os.path.join(hid_dir, "hidraw", "hidraw*"))
    return sorted(f"/dev/{os.path.basename(n)}" for n in nodes)

def hidraw_gate(action, uniq, nodes):
    """action: 'block' | 'restore' | 'rearm', keyed by the pad's stable HID
    identity (uniq: BT MAC / serial). The v2 helper actually revokes access —
    a plain chmod cannot: a process that opened the node before the gate
    (Steam Input grabs every pad on connect) keeps a working fd. The helper
    rebinds the kernel driver on a state transition, which kills every open
    fd and re-probes the pad (resetting the DualSense lightbar latch); the
    device nodes RENUMBER when that happens, so callers must re-resolve them
    afterwards (ControllerInstance._refresh_nodes). Pads without a uniq fall
    back to the v1 per-node chmod (best effort, no revocation). No-op when
    the helper/sudo rule is missing."""
    if not os.path.exists(GATE_BIN):
        return
    if uniq:
        cmds = [["sudo", "-n", GATE_BIN, action, uniq]]
    else:
        legacy = {"block": "block-node", "restore": "restore-node"}.get(action)
        if not legacy or not nodes:
            return
        cmds = [["sudo", "-n", GATE_BIN, legacy, node] for node in nodes]
    for cmd in cmds:
        try:
            # timeout 10: a driver rebind (unbind + re-probe) is slower than
            # the old chmod; still far below anything a user would notice as
            # a hang, and generous enough for a busy USB/BT stack.
            subprocess.run(
                cmd, timeout=10, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as ex:
            print(f"hidraw_gate: {' '.join(cmd[2:])} failed: {ex}",
                  file=sys.stderr)


def led_indicator_for_event(ev_path):
    """Return the DualSense RGB-indicator LED name (e.g. 'input38:rgb:indicator')
    for the HID device behind an evdev node, or None. The hid-playstation driver
    exposes the lightbar as a multicolor LED; driving it there lets the kernel
    do the USB/BT output-report framing, so the colour survives BT reconnects —
    unlike raw hidraw writes, which race the driver and get dropped over BT."""
    if not ev_path:      # instance mid-rebind: node not re-resolved yet
        return None
    base = f"/sys/class/input/{os.path.basename(ev_path)}/device"
    if not os.path.exists(base):
        return None
    hid_dir = os.path.realpath(os.path.join(base, "..", ".."))
    nodes = glob.glob(os.path.join(hid_dir, "leds", "*:rgb:indicator"))
    return os.path.basename(nodes[0]) if nodes else None

def led_set(led, rgb):
    """Set the lightbar colour via the kernel LED class (root helper).
    No-op when there is no such LED or the helper/sudo rule is missing."""
    if not led or not os.path.exists(LED_BIN):
        return
    r, g, b = rgb
    try:
        subprocess.run(
            ["sudo", "-n", LED_BIN, led, str(r), str(g), str(b)],
            timeout=5, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as ex:
        print(f"led_set: {led} failed: {ex}", file=sys.stderr)


def led_set_player(prefix, player):
    """Light the pad's white player LEDs (hid-playstation …:white:player-N)
    to the daemon's stable player number via the root helper. The pattern is
    PS5-authentic: the lit-LED count equals the player number. No-op without
    a helper, a prefix or a representable number (patterns cover 1-4)."""
    if not prefix or not player or player > 4 or not os.path.exists(LED_BIN):
        return
    try:
        subprocess.run(
            ["sudo", "-n", LED_BIN, "player", prefix, str(player)],
            timeout=5, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as ex:
        print(f"led_set_player: {prefix} failed: {ex}", file=sys.stderr)


def hidraw_holders(nodes):
    """PIDs of other processes holding an open fd on any of the given
    /dev/hidrawN nodes. Only same-user processes are visible without
    privileges — which is exactly the population that matters here (Steam,
    games, launchers all run as the desktop user)."""
    holders = set()
    if not nodes:
        return holders
    want = set(nodes)
    me = str(os.getpid())
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit() and p != me]
    except OSError:
        return holders
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(f"{fd_dir}/{fd}") in want:
                        holders.add(int(pid))
                        break
                except OSError:      # fd closed while we looked
                    continue
        except OSError:              # process exited / not ours
            continue
    return holders


# ── Controller discovery ─────────────────────────────────────────────────────

def ident_of(vendor, product, uniq, phys=""):
    """Stable identity of a physical pad across reconnects and node churn:
    the BT MAC / serial when the pad reports one; else the physical
    attachment point (distinguishes two IDENTICAL uniq-less pads, e.g. two
    USB Xbox pads on xpad — stable within a session, though not across a
    port change); else vendor:product as the last resort."""
    if uniq:
        return uniq
    if phys:
        return f"phys:{phys}"
    return f"{vendor:04x}:{product:04x}"


def scan_controllers(exclude_paths=frozenset()):
    """All connected real controllers as list of dicts. Shared by the
    manager's poll and by a single instance re-resolving its nodes after a
    gate transition, so both apply the exact same notion of 'a controller'."""
    result = []
    for path in evdev.list_devices():
        if path in exclude_paths:
            continue
        try:
            dev = evdev.InputDevice(path)
        except Exception:
            continue
        try:
            phys = dev.phys or ""
            # Our own virtual output pads: python-evdev stamps every uinput
            # device it creates with its phys marker, so that — not the name —
            # is the reliable tell. The name may only exclude when phys is
            # empty: xpad names a REAL wired Xbox 360 pad exactly like
            # VIRTUAL_XBOX, but a physical pad always carries its usb/bt
            # attachment in phys, so it stays adoptable.
            if phys == "py-evdev-uinput" or (not phys and dev.name in VIRTUAL_NAMES):
                continue
            # Software-emulated pads: Sunshine (inputtino) creates virtual
            # DualSense/Xbox pads over uhid with a REAL Sony/Microsoft VID for
            # its stream clients. Adopting one would grab and remap a stream
            # client's own input. They are recognisable by their synthetic
            # phys and their '(virtual)' kernel device name.
            if phys == "INPUTTINO_BT_LINK" or "(virtual)" in dev.name:
                continue
            family = VENDOR_FAMILY.get(dev.info.vendor)
            if not family:
                continue
            # Must be an actual gamepad — excludes headset / motion-sensor /
            # touchpad / consumer-control nodes that share the same vendor id.
            keys = dev.capabilities().get(e.EV_KEY, [])
            if e.BTN_GAMEPAD not in keys and e.BTN_SOUTH not in keys:
                continue
            name = CONTROLLER_NAMES.get(
                (dev.info.vendor, dev.info.product), dev.name)
            result.append({
                "path":    path,
                "name":    name,
                "vendor":  dev.info.vendor,
                "product": dev.info.product,
                "family":  family,
                "uniq":    dev.uniq or "",
                "phys":    phys,
                "hidraw":  hidraw_for_event(path),
            })
        finally:
            dev.close()
    return result


# ── Remapper thread ──────────────────────────────────────────────────────────

class Remapper(threading.Thread):
    """Grabs a source controller and emits a virtual target device."""

    def __init__(self, src_path, target_spec, button_map=None):
        super().__init__(daemon=True)
        self._src_path   = src_path
        self._target     = target_spec
        self._button_map = button_map or {}
        self._stop_event = threading.Event()
        self._ui         = None
        self._virtual_path = None
        # Self-pipe to wake the read loop deterministically on stop(). Closing
        # the source fd from another thread does NOT reliably interrupt a read()
        # blocked on an idle device (Linux), which previously leaked the thread
        # and its virtual uinput device. select() on this pipe avoids that.
        self._wake_r, self._wake_w = os.pipe()

    @property
    def virtual_path(self):
        return self._virtual_path

    def stop(self):
        self._stop_event.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def run(self):
        try:
            src = evdev.InputDevice(self._src_path)
        except Exception as ex:
            print(f"remapper: cannot open {self._src_path}: {ex}", file=sys.stderr)
            return

        caps = src.capabilities()
        caps.pop(e.EV_SYN, None)
        caps.pop(e.EV_FF,  None)

        # Advertise the translated (standard) key codes, else SDL maps the
        # target identity against the source's non-standard codes.
        if self._button_map and e.EV_KEY in caps:
            caps[e.EV_KEY] = list(dict.fromkeys(
                self._button_map.get(c, c) for c in caps[e.EV_KEY]))

        try:
            ui = UInput(events=caps, **self._target)
        except Exception as ex:
            print(f"remapper: cannot create uinput: {ex}", file=sys.stderr)
            return

        self._ui           = ui
        self._virtual_path = ui.device.path

        try:
            src.grab()
        except Exception as ex:
            print(f"remapper: grab failed for {self._src_path}: {ex}", file=sys.stderr)
            ui.close()
            return

        print(f"remapper: {src.name} → {self._target['name']} "
              f"({self._virtual_path})", file=sys.stderr)

        sel = selectors.DefaultSelector()
        sel.register(src.fileno(), selectors.EVENT_READ)
        sel.register(self._wake_r,  selectors.EVENT_READ)
        try:
            while not self._stop_event.is_set():
                for key, _ in sel.select():
                    if key.fd == self._wake_r:        # stop() signalled
                        self._stop_event.set()
                        break
                    for event in src.read():          # all currently queued events
                        if event.type in (e.EV_KEY, e.EV_ABS, e.EV_REL):
                            code = event.code
                            if event.type == e.EV_KEY and self._button_map:
                                code = self._button_map.get(code, code)
                            ui.write(event.type, code, event.value)
                            ui.syn()
        except OSError:
            pass
        finally:
            sel.close()
            try: src.ungrab()
            except Exception: pass
            try: src.close()
            except Exception: pass
            try: ui.close()
            except Exception: pass
            try: os.close(self._wake_r)
            except Exception: pass
            try: os.close(self._wake_w)
            except Exception: pass
            self._virtual_path = None
            print(f"remapper: stopped for {self._src_path}", file=sys.stderr)


# ── Controller instance ──────────────────────────────────────────────────────

class ControllerInstance:
    def __init__(self, path, name, vendor, product, family, mode, uniq, phys,
                 hidraw):
        self.path    = path
        self.name    = name
        self.vendor  = vendor
        self.product = product
        self.family  = family
        self.mode    = mode
        self.uniq    = uniq       # stable per-device id (BT MAC / serial)
        self.phys    = phys       # physical attachment (USB port / BT adapter)
        # Stable key across reconnects: a reconnect changes the evdev path but
        # not this, so we can recognise the same physical pad and rebind it
        # instead of churning the tray menu.
        self.ident   = ident_of(vendor, product, uniq, phys)
        self.hidraw  = hidraw     # list of /dev/hidrawN (may be empty)
        # DualSense lightbar via the kernel LED class (…:rgb:indicator); None for
        # Xbox pads or when the node can't be resolved. Re-resolved per instance,
        # so it tracks the node renumbering that happens on every BT reconnect.
        self.led     = led_indicator_for_event(path) if family == "ps5" else None
        # Stable player number in overall connection order (first-connected
        # pad = 1), assigned by the manager at adoption and freed on removal.
        # Shown on the DualSense white player LEDs and in the tray label —
        # deliberately OURS, not the kernel's: the kernel re-allocates its id
        # on every gate rebind, and Steam renumbers its slots on every
        # (re)enumeration, so both drift under mode switches.
        self.player  = None
        self._remap  = None
        self._gone_since = None   # monotonic time first seen absent, for grace
        # Resting-colour bookkeeping (ps5-native only, see watch_holders):
        self._holders      = set()   # hidraw holder pids last tick
        self._settle_until = 0.0     # until then, new holders are reopen noise
        self._repaint_at   = None    # scheduled one-shot resting repaint
        self._player_repaint_at = None   # one-shot player-LED-only re-assert

    def display_name(self):
        return self.name

    # ── LED lightbar ─────────────────────────────────────────────────────────

    def _apply_led(self):
        """Set the lightbar colour for the current mode via the kernel LED class
        (hid-playstation …:rgb:indicator). Re-resolves the node from the live
        evdev path on every call and never trusts the cached name: the LED node is
        renumbered on every (BT) reconnect, so a stale name would make the write
        land on a now-dead node. No-op for Xbox pads or when no node resolves."""
        if self.family != "ps5":
            return
        led = led_indicator_for_event(self.path)
        if not led:
            return
        self.led = led
        led_set(led, MODE_LED.get(self.mode, (0, 0, 0)))
        self._apply_player_leds()

    def _apply_player_leds(self):
        """Re-assert this pad's player number on its white player LEDs. Same
        churn problem as the lightbar: the kernel re-allocates its player id
        on every gate rebind and Steam rewrites its own slot count raw on
        every (re)enumeration — the daemon's adoption-ordered number is the
        one that stays put. The LED names share the pad's live inputN prefix
        with the rgb indicator, so resolve it from there."""
        if self.family != "ps5" or not self.player:
            return
        led = led_indicator_for_event(self.path)
        if led:
            led_set_player(led.split(":", 1)[0], self.player)

    def refresh_led(self):
        """Repaint the lightbar when its LED node has renumbered under us. The
        …:rgb:indicator node is named after the inputN instance, which bumps on
        every (BT) reconnect *even when the /dev/input/eventX path is reused* — so
        the path-keyed rebind() never sees the change and the daemon would keep
        painting the old, now-dead node (a mode switch then changes no colour).
        Called each monitor tick: when the live node differs from the one we last
        painted, repaint it. Idempotent while the node is stable."""
        if self.family != "ps5":
            return
        led = led_indicator_for_event(self.path)
        if led and led != self.led:
            self._apply_led()

    def watch_holders(self):
        """Resting-colour policy for ps5-native (remap modes need none of
        this — there the gate guarantees exclusive LED ownership). Three
        kinds of raw-HID holders exist, told apart by the holder-set delta:

        * a RELEASER (game exited): it may leave the lightbar firmware-
          latched dark ('light out' setup flag — kernel LED writes then
          change nothing until the driver re-probes), so rearm (driver
          rebind → fresh lightbar setup) and repaint the resting colour;
        * a REOPENER (Steam enumerating the pad right after one of our
          rebinds — it opens even with its PlayStation support disabled):
          writes its defaults ONCE and goes quiet, so schedule one delayed
          repaint to get the last word (field-verified: Steam reopened ~6 s
          after a rebind and stomped the freshly painted colour);
        * a genuinely NEW holder outside the settle window (a game started):
          the lightbar is legitimately theirs — hands off, cancel repaints.

        The scheduled one-shot repaint at the end fires in remap modes too:
        apply_mode arms it after every gate transition, because the write
        right after a driver rebind races the BT re-probe and can be dropped.
        Called from the monitor tick, outside the manager lock (shells out)."""
        if self.family != "ps5":
            return
        now = time.monotonic()
        if self._target_for_mode() is None:
            holders = hidraw_holders(self.hidraw)
            if holders != self._holders:
                released = self._holders - holders
                self._holders = holders
                if released:
                    hidraw_gate("rearm", self.uniq, self.hidraw)   # kills fds too
                    self._holders = set()          # we just revoked the rest
                    self._refresh_nodes()
                    self._apply_led()
                    self._settle_until = now + 20.0
                    self._repaint_at   = now + 6.0
                elif now < self._settle_until:
                    self._repaint_at = now + 6.0   # outlast the reopen write
                else:
                    self._repaint_at = None        # game took the pad on purpose
        if self._repaint_at is not None and now >= self._repaint_at:
            self._repaint_at = None
            self._apply_led()
        # Player-LED-only re-assert, armed by the manager when ANOTHER pad's
        # gate churn made Steam recount and rewrite its slots on every pad it
        # holds. Deliberately does not touch the lightbar: outside the settle
        # window that may legitimately belong to a game.
        if (self._player_repaint_at is not None
                and now >= self._player_repaint_at):
            self._player_repaint_at = None
            self._apply_player_leds()

    # ── mode / lifecycle ──────────────────────────────────────────────────────

    def _target_for_mode(self):
        if self.mode == "ps5-xbox":
            return VIRTUAL_XBOX
        if self.mode == "xbox-ps5":
            return VIRTUAL_PS5
        return None

    def apply_mode(self, adopt=False):
        """Stop existing remapper, transition the hidraw gate, start a new
        remapper if needed, update LED. A gate state transition rebinds the
        kernel driver (to revoke foreign fds and reset the lightbar latch),
        which renumbers this pad's device nodes — so re-resolve them before
        grabbing, and never grab before gating (the rebind would kill the
        grab again)."""
        if self._remap:
            old = self._remap
            old.stop()
            old.join(timeout=1.0)   # wait for ungrab before the new grab attempt
            self._remap = None

        target = self._target_for_mode()
        # Hide this pad's raw HID path from applications before the remapper
        # takes over (the evdev grab alone does not cover hidraw) — or reopen
        # it when returning to native.
        hidraw_gate("block" if target else "restore", self.uniq, self.hidraw)
        self._refresh_nodes()
        if adopt and not target and hidraw_holders(self.hidraw):
            # Adoption found the pad already opened by someone else: that fd
            # predates us (Steam Input grabs every pad the moment it connects)
            # and may have firmware-latched the lightbar dark — a state no
            # later kernel LED write can clear. Gating relies on marker
            # TRANSITIONS to rebind, and a freshly paired identity has no
            # marker, so the restore above never rebound — force it here.
            # Holders that arrive THROUGH a restore rebind never hit this:
            # their nodes are renumbered and reopen only seconds later.
            hidraw_gate("rearm", self.uniq, self.hidraw)
            self._refresh_nodes()

        if target and self.path:
            bmap = compose_button_maps(
                QUIRK_BUTTON_MAP.get((self.vendor, self.product)),
                TARGET_BUTTON_MAP.get(target["name"]))
            r = Remapper(self.path, target, bmap)
            r.start()
            self._remap = r

        # The gate transition above may have rebound the driver: every old
        # holder fd is dead, and enumerators (Steam) will REOPEN the reborn
        # nodes in a few seconds and write their defaults once — open the
        # settle window so watch_holders schedules the outlasting repaint
        # instead of mistaking the reopen for a game start. The repaint is
        # also scheduled unconditionally: the immediate write below races the
        # BT re-probe after a rebind and can be dropped by the pad — the
        # delayed re-assert is what makes the colour stick (in remap modes
        # too, where the holder policy itself is inactive).
        self._holders      = set()
        self._settle_until = time.monotonic() + 20.0
        self._repaint_at   = time.monotonic() + 6.0

        self._apply_led()

    def _refresh_nodes(self, timeout=3.0):
        """Re-resolve path/hidraw/LED bindings after a gate transition tore
        the pad's kernel nodes down and recreated them renumbered. Polls
        briefly because the re-probe takes a moment; when nothing was rebound
        the first scan simply confirms the existing binding. If the pad does
        not come back in time the stale path is dropped — the monitor's
        reconcile rebinds when it reappears."""
        deadline = time.monotonic() + timeout
        while True:
            for d in scan_controllers():
                if ident_of(d["vendor"], d["product"],
                            d["uniq"], d["phys"]) == self.ident:
                    self.path   = d["path"]
                    self.name   = d["name"]
                    self.hidraw = d["hidraw"]
                    self.led    = (led_indicator_for_event(self.path)
                                   if self.family == "ps5" else None)
                    return
            if time.monotonic() >= deadline:
                self.path = None
                return
            time.sleep(0.1)

    def rebind(self, path, name, hidraw):
        """Same physical pad reappeared on a new evdev/hidraw node after a (BT)
        reconnect. Refresh the node bindings and restart the remapper in place —
        the instance (and thus its tray-menu entry) is kept, so the host sees no
        structural change. Deliberately NO gate 'restore' here: the gate is
        keyed by the pad's stable identity, so staying gated across a reconnect
        is exactly right — the reborn nodes were already born gated (udev
        rule), and apply_mode re-asserts the gate idempotently."""
        self.path   = path
        self.name   = name
        self.hidraw = hidraw
        self.led    = led_indicator_for_event(path) if self.family == "ps5" else None
        self.apply_mode()                     # re-grab, re-gate, re-apply LED

    def virtual_path(self):
        return self._remap.virtual_path if self._remap else None

    def remap_healthy(self):
        """False when the current mode calls for a remapper but none is
        running (thread died: lost grab after a sub-poll BT blip, or the
        grab failed outright). Native modes are trivially healthy."""
        if self._target_for_mode() is None:
            return True
        return self._remap is not None and self._remap.is_alive()

    def stop(self):
        if self._remap:
            self._remap.stop()
            self._remap = None
        # Ungate whenever we stop managing this pad (disconnect or shutdown),
        # so a gated pad never lingers invisible to every application.
        hidraw_gate("restore", self.uniq, self.hidraw)


# ── Controller Manager ───────────────────────────────────────────────────────

# Grace period before a vanished controller is really dropped. A Bluetooth pad
# re-registers on a *new* evdev/hidraw node after every reconnect (e.g. when it
# is un/re-paired for console use); without this window the brief gap would tear
# the instance down and rebuild it, churning the tray menu into stuck-disabled
# items. Instances are keyed by stable identity (BT MAC / serial), so a pad that
# returns within the window is rebound in place instead of removed and re-added.
REMOVE_GRACE = 5.0   # seconds; ~2× the 2 s poll interval

class ControllerManager:
    def __init__(self, on_change_cb):
        self._lock       = threading.Lock()
        self._instances  = {}      # ident → ControllerInstance
        self._config     = load_config()
        self._on_change  = on_change_cb   # called (from thread) when list changes
        self._monitor_th = threading.Thread(target=self._monitor, daemon=True)

    def start(self):
        # Populate instances synchronously so the first menu we publish already
        # reflects connected controllers. Registering an empty menu and mutating
        # it afterwards makes the host recycle item ids across a structural change
        # (separator→radio, header relabel), which leaves entries stuck disabled.
        # Then run the periodic monitor for hotplug.
        self._poll()
        self._monitor_th.start()

    def _virtual_paths(self):
        paths = set()
        for inst in self._instances.values():
            vp = inst.virtual_path()
            if vp:
                paths.add(vp)
        return paths

    def _scan(self):
        """Connected real controllers, minus our own virtual output devices."""
        return scan_controllers(exclude_paths=self._virtual_paths())

    @staticmethod
    def _ident(d):
        """Stable identity of a scanned controller — matches ControllerInstance.ident."""
        return ident_of(d["vendor"], d["product"], d["uniq"], d["phys"])

    def _poll(self):
        """One scan/reconcile pass; returns True if the *set* of controllers
        changed. A reconnect that only moves a pad to a new node does not count —
        it is rebound in place, so the tray menu is left untouched."""
        found = {}
        for d in self._scan():
            found[self._ident(d)] = d   # identical padless pads collapse; acceptable
        changed = False
        churned = False       # any gate/driver churn Steam re-enumerates on
        config_dirty = False  # a player number was assigned or changed
        now = time.monotonic()
        with self._lock:
            # Reconcile known instances: rebind on reconnect, drop after grace.
            for ident, inst in list(self._instances.items()):
                d = found.get(ident)
                if d is None:                       # absent this pass
                    if inst._gone_since is None:
                        inst._gone_since = now
                    elif now - inst._gone_since >= REMOVE_GRACE:
                        inst.stop()
                        del self._instances[ident]
                        changed = True
                    continue
                was_gone = inst._gone_since is not None
                inst._gone_since = None             # present again
                if d["path"] != inst.path:          # reconnected on a new node
                    inst.rebind(d["path"], d["name"], d["hidraw"])
                    churned = True
                elif was_gone or not inst.remap_healthy():
                    # Two ways a pad can look unchanged yet be broken underneath:
                    #  * reconnected on the *reused* evdev path (BT re-pair /
                    #    power-cycle): same eventX number, fresh device below —
                    #    the old grab died and the firmware reset the lightbar;
                    #  * a BT blip shorter than one poll killed the remapper's
                    #    grab without the pad ever appearing absent (the thread
                    #    is dead but the mode still wants a remap).
                    # Both: re-assert the whole mode — restart the remapper,
                    # re-gate the hidraw node, repaint the lightbar.
                    inst.rebind(d["path"], d["name"], d["hidraw"])
                    churned = True
            # Add genuinely new controllers.
            for ident, d in found.items():
                if ident in self._instances:
                    continue
                default = DEFAULT_MODES.get(d["family"], "ps5-native")
                mode = self._config.get(ident, default)
                # Drop modes no longer offered for this family (e.g. a stored
                # "xbox-ps5" from before it was removed): fall back to the native
                # default instead of silently applying an unselectable mode.
                if mode not in MODES_FOR_FAMILY.get(d["family"], []):
                    mode = default
                inst = ControllerInstance(
                    d["path"], d["name"], d["vendor"], d["product"],
                    d["family"], mode, d["uniq"], d["phys"], d["hidraw"])
                # Overall connection order: a pad keeps the number it was
                # FIRST adopted under ("_players" in the config, keyed like
                # the modes by stable ident) — daemon restarts re-adopt in
                # evdev node order, which the gate rebinds shuffle, so the
                # session order alone would swap numbers between restarts.
                # Fresh pads (or a persisted number currently worn by another
                # connected pad) take the lowest free number.
                used = {i.player for i in self._instances.values()}
                player = self._config.get("_players", {}).get(ident)
                if not player or player in used:
                    player = next(p for p in range(1, len(used) + 2)
                                  if p not in used)
                inst.player = player
                if self._config.get("_players", {}).get(ident) != player:
                    self._config.setdefault("_players", {})[ident] = player
                    config_dirty = True
                inst.apply_mode(adopt=True)
                self._instances[ident] = inst
                changed = True
                churned = True
            if churned:
                self._arm_player_reassert()
        if config_dirty:
            save_config(self._config)
        if changed:
            GLib.idle_add(self._on_change)
        # LED self-heal + resting-colour watch, outside the lock (both may
        # shell out to sudo, and we must not stall set_mode/get_instances
        # behind that): a pad rebound while its :rgb:indicator node still
        # lagged gets its colour applied on a later tick, and a ps5-native pad
        # whose last raw-HID holder (Steam / a game) just exited gets rearmed
        # and repainted to the resting blue.
        for inst in self.get_instances():
            inst.refresh_led()
            inst.watch_holders()
        return changed

    def _monitor(self):
        while True:
            time.sleep(2)
            self._poll()

    def get_instances(self):
        with self._lock:
            return list(self._instances.values())

    def _arm_player_reassert(self):
        """A gate/driver rebind makes enumerators (Steam) recount and rewrite
        THEIR player slots raw on every pad they hold — not just the rebound
        one. Schedule a one-shot re-assert of our stable numbers on all ps5
        pads once that write has passed. Called under the manager lock."""
        for inst in self._instances.values():
            if inst.family == "ps5":
                inst._player_repaint_at = time.monotonic() + 6.0

    def set_mode(self, ident, mode):
        with self._lock:
            inst = self._instances.get(ident)
            if not inst:
                return
            inst.mode = mode
            inst.apply_mode()
            self._config[inst.ident] = mode
            self._arm_player_reassert()
        save_config(self._config)
        GLib.idle_add(self._on_change)

    def stop_all(self):
        with self._lock:
            for inst in self._instances.values():
                inst.stop()


# ── dbusmenu ────────────────────────────────────────────────────────────────

class DbusmenuServer(dbus.service.Object):

    def __init__(self, bus, path, manager, on_quit):
        dbus.service.Object.__init__(self, bus, path)
        self._mgr      = manager
        self._on_quit  = on_quit
        self._revision = 1
        # Cached menu model. Every dbusmenu call (GetLayout, GetGroupProperties,
        # GetProperty, Event) answers from this one snapshot, so within a
        # revision a host can never observe two different id→item mappings —
        # rebuilding per call let the instance list shift between a host's
        # GetLayout and its Event, misrouting the click.
        self._items    = []    # ordered [(id, props-dict)], Quit last
        self._lookup   = {}    # id → (controller ident, mode) for radio items
        self._sig      = None  # structural signature of the cached model
        # Item ids come from this counter and are NEVER reused for a different
        # item. The GNOME appindicator host caches item properties per id and
        # does not refresh them when a layout change reuses an id for a
        # structurally different item (separator→radio after a controller
        # connects), leaving entries stuck disabled. Fresh ids per structural
        # change force the host to treat them as new items and fetch fresh
        # properties. Display-only changes keep their ids and are pushed as
        # ItemsPropertiesUpdated deltas instead.
        self._next_id  = 1

    # ── menu building ────────────────────────────────────────────────────────

    def _make_item(self, id_, props):
        return dbus.Struct(
            [dbus.Int32(id_),
             dbus.Dictionary(props, signature="sv"),
             dbus.Array([], signature="v")],
            signature="(ia{sv}av)"
        )

    def _inst_label(self, inst, instances):
        """'DualSense' for a unique model; 'DualSense <player>' when duplicates
        exist. The player number is the same one shown on the pad's white
        player LEDs, so menu and hardware always agree — a positional index
        would flip after a drop-and-readopt while the LEDs kept the number."""
        peers = [i for i in instances if i.name == inst.name]
        if len(peers) == 1:
            return inst.name
        if inst.player:
            return f"{inst.name} {inst.player}"
        return f"{inst.name} {peers.index(inst) + 1}"

    def _semantic_items(self):
        """The menu as plain (kind, …) tuples, before ids and dbus types:
        ('info', label) | ('header', ident, label)
        | ('radio', ident, mode, label, checked) | ('sep',)."""
        instances = self._mgr.get_instances()
        sem = []
        if not instances:
            sem.append(("info", "No controller connected"))
        else:
            for inst in instances:
                sem.append(("header", inst.ident,
                            self._inst_label(inst, instances)))
                for mode in MODES_FOR_FAMILY.get(inst.family, []):
                    sem.append(("radio", inst.ident, mode,
                                MODE_LABELS[mode], inst.mode == mode))
                if inst is not instances[-1]:
                    sem.append(("sep",))
        sem.append(("sep",))   # final separator before Quit
        return sem

    @staticmethod
    def _props_for(entry):
        kind = entry[0]
        if kind == "info":
            return {"label":   dbus.String(entry[1]),
                    "enabled": dbus.Boolean(False)}
        if kind == "header":
            return {"label":   dbus.String(entry[2]),
                    "enabled": dbus.Boolean(False)}
        if kind == "radio":
            return {"label":        dbus.String(entry[3]),
                    "enabled":      dbus.Boolean(True),
                    "toggle-type":  dbus.String("radio"),
                    "toggle-state": dbus.Int32(1 if entry[4] else 0)}
        return {"type": dbus.String("separator")}

    @staticmethod
    def _structure_sig(sem):
        """What defines an item's KIND and click target. Labels and
        toggle-state are volatile display state — excluded here, so they
        update in place via ItemsPropertiesUpdated without an id change."""
        sig = []
        for entry in sem:
            if entry[0] == "radio":
                sig.append(("radio", entry[1], entry[2]))
            elif entry[0] == "header":
                sig.append(("header", entry[1]))
            else:
                sig.append((entry[0],))
        return tuple(sig)

    def _rebuild(self):
        """Sync the cached model to the current controller state and emit the
        matching dbusmenu signal (LayoutUpdated on structural change,
        ItemsPropertiesUpdated for display-state deltas). Main loop only."""
        sem = self._semantic_items()
        sig = self._structure_sig(sem)

        if sig != self._sig:
            items, lookup = [], {}
            for entry in sem:
                if self._next_id == QUIT_ID:   # never hand out the Quit id
                    self._next_id += 1
                id_ = self._next_id
                self._next_id += 1
                items.append((id_, self._props_for(entry)))
                if entry[0] == "radio":
                    lookup[id_] = (entry[1], entry[2])
            # Quit is the one constant item: same kind, label and props
            # forever, so its well-known id is safe to keep.
            items.append((QUIT_ID, {"label":   dbus.String("Quit"),
                                    "enabled": dbus.Boolean(True)}))
            self._items, self._lookup, self._sig = items, lookup, sig
            self._revision += 1
            self.LayoutUpdated(dbus.UInt32(self._revision), dbus.Int32(0))
            return

        # Same structure: positions align pairwise with the cached items
        # (Quit, cached last, has no semantic entry — zip stops before it).
        updated = []
        for (id_, props), entry in zip(self._items, sem):
            new = self._props_for(entry)
            delta = {k: v for k, v in new.items() if props.get(k) != v}
            if delta:
                props.update(delta)
                updated.append((id_, delta))
        if updated:
            self.ItemsPropertiesUpdated(
                dbus.Array(
                    [dbus.Struct(
                        [dbus.Int32(i), dbus.Dictionary(p, signature="sv")],
                        signature="(ia{sv})") for i, p in updated],
                    signature="(ia{sv})"),
                dbus.Array([], signature="(ias)"))

    def _layout(self):
        if self._sig is None:      # first host call before any state change
            self._rebuild()
        return dbus.Struct(
            [dbus.Int32(0),
             dbus.Dictionary({}, signature="sv"),
             dbus.Array(
                 [dbus.Struct(self._make_item(id_, props),
                              signature="(ia{sv}av)")
                  for id_, props in self._items],
                 signature="v")],
            signature="(ia{sv}av)"
        )

    def notify_update(self):
        self._rebuild()

    # ── dbusmenu methods ─────────────────────────────────────────────────────

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parentId, recursionDepth, propertyNames):
        return dbus.UInt32(self._revision), self._layout()

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, propertyNames):
        if self._sig is None:
            self._rebuild()
        want  = {int(i) for i in ids}            # empty → all items, per spec
        names = {str(n) for n in propertyNames}  # empty → all properties
        result = []
        for id_, props in self._items:
            if want and id_ not in want:
                continue
            if names:
                props = {k: v for k, v in props.items() if k in names}
            result.append(dbus.Struct(
                [dbus.Int32(id_), dbus.Dictionary(props, signature="sv")],
                signature="(ia{sv})"))
        return dbus.Array(result, signature="(ia{sv})")

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="is", out_signature="v")
    def GetProperty(self, id_, name):
        if self._sig is None:
            self._rebuild()
        for iid, props in self._items:
            if iid == int(id_) and str(name) in props:
                return props[str(name)]
        return dbus.String("")

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="isvu", out_signature="")
    def Event(self, id_, eventId, data, timestamp):
        if eventId != "clicked":
            return
        if id_ == QUIT_ID:
            GLib.idle_add(lambda: (self._on_quit(), False)[1])
            return
        # Route via the cached model, so the click lands on exactly the item
        # the host displayed. An id from a stale revision (structure changed
        # since the host fetched it) is simply absent here and ignored.
        hit = self._lookup.get(int(id_))
        if hit:
            ctrl_ident, mode = hit
            self._mgr.set_mode(ctrl_ident, mode)

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="a(isvu)", out_signature="ai")
    def EventGroup(self, events):
        for id_, eventId, data, ts in events:
            self.Event(id_, eventId, data, ts)
        return dbus.Array([], signature="i")

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="i", out_signature="b")
    def AboutToShow(self, id_):
        return False

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="ai", out_signature="aiai")
    def AboutToShowGroup(self, ids):
        return dbus.Array([], "i"), dbus.Array([], "i")

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return {"Version": dbus.UInt32(3), "TextDirection": dbus.String("ltr"),
                "Status":  dbus.String("normal"),
                "IconThemePath": dbus.Array([], "s")}.get(prop, dbus.String(""))

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return {"Version": dbus.UInt32(3), "TextDirection": dbus.String("ltr"),
                "Status":  dbus.String("normal"),
                "IconThemePath": dbus.Array([], "s")}

    @dbus.service.signal("com.canonical.dbusmenu", signature="ui")
    def LayoutUpdated(self, revision, parent): pass

    @dbus.service.signal("com.canonical.dbusmenu", signature="a(ia{sv})a(ias)")
    def ItemsPropertiesUpdated(self, updated, removed): pass

    @dbus.service.signal("com.canonical.dbusmenu", signature="iu")
    def ItemActivationRequested(self, id_, timestamp): pass


# ── StatusNotifierItem (tray) ────────────────────────────────────────────────

class TrayIcon(dbus.service.Object):

    def __init__(self, bus, manager, menu):
        dbus.service.Object.__init__(self, bus, ITEM_PATH)
        self._mgr  = manager
        self._menu = menu

    def _props(self):
        instances = self._mgr.get_instances()
        active_remaps = [i for i in instances
                         if i.mode not in ("ps5-native", "xbox-native")]
        if active_remaps:
            icon  = "input-gaming"
        else:
            icon  = "input-gaming-symbolic"
        title = "Controller Manager"
        if instances:
            tip = "\n".join(
                f"{inst.name} — {MODE_LABELS[inst.mode]}" for inst in instances)
        else:
            tip = "No controller connected"

        return {
            "Category":      dbus.String("Hardware"),
            "Id":            dbus.String("controller-manager"),
            "Status":        dbus.String("Active"),
            "Title":         dbus.String(title),
            "IconName":      dbus.String(icon),
            "IconThemePath": dbus.String(""),
            "Menu":          dbus.ObjectPath(MENU_PATH),
            "ItemIsMenu":    dbus.Boolean(True),
            "ToolTip":       dbus.Struct(
                [dbus.String(icon), dbus.Array([], "(iiay)"),
                 dbus.String(title), dbus.String(tip)],
                signature="(sa(iiay)ss)"
            ),
        }

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self._props().get(prop, dbus.String(""))

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self._props()

    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii")
    def Activate(self, x, y): pass

    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii")
    def ContextMenu(self, x, y): pass

    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii")
    def SecondaryActivate(self, x, y): pass

    def refresh(self):
        self._menu.notify_update()
        self.NewIcon()
        self.NewTitle()
        self.NewToolTip()

    @dbus.service.signal("org.kde.StatusNotifierItem", signature="s")
    def NewStatus(self, s): pass

    @dbus.service.signal("org.kde.StatusNotifierItem")
    def NewIcon(self): pass

    @dbus.service.signal("org.kde.StatusNotifierItem")
    def NewTitle(self): pass

    @dbus.service.signal("org.kde.StatusNotifierItem")
    def NewToolTip(self): pass


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    loop = GLib.MainLoop()
    bus  = dbus.SessionBus()
    # Own the conventional StatusNotifierItem well-known name, held for the
    # process lifetime (an unreferenced BusName would be garbage-collected and
    # the name released). Registration itself goes by object path rather than
    # this name (see register_with_watcher); owning it just gives a stable,
    # spec-conventional identity for hosts that look items up by name.
    bus_name = dbus.service.BusName(BUS_NAME, bus)

    tray_ref = [None]

    def on_change():
        if tray_ref[0]:
            tray_ref[0].refresh()

    mgr  = ControllerManager(on_change)

    def _shutdown_and_quit():
        mgr.stop_all()
        loop.quit()

    menu = DbusmenuServer(bus, MENU_PATH, mgr, _shutdown_and_quit)
    tray = TrayIcon(bus, mgr, menu)
    tray_ref[0] = tray

    watcher_name = "org.kde.StatusNotifierWatcher"

    def register_with_watcher():
        try:
            watcher = bus.get_object(watcher_name, "/StatusNotifierWatcher")
            # Register by object path, not by our well-known bus name. The host
            # keys the item on the registration argument: a well-known name is
            # stable across restarts, so a fresh process collides with the dying
            # previous indicator (which the host only tears down ~500ms after our
            # old connection drops) and the registration is silently swallowed.
            # The path form keys on our *unique* connection name instead, so each
            # run is distinct and the stale indicator cleans itself up.
            watcher.RegisterStatusNotifierItem(
                ITEM_PATH, dbus_interface=watcher_name)
        except Exception as ex:
            print(f"controller-manager: watcher registration failed: {ex}",
                  file=sys.stderr)

    # Populate controllers BEFORE announcing ourselves, so the first menu the
    # host reads is already complete (see ControllerManager.start).
    mgr.start()

    # Register exactly once per watcher lifetime, driven by watch_name_owner.
    # Two points matter:
    #   * It must run inside the main loop (watch_name_owner delivers the current
    #     owner asynchronously, i.e. once loop.run() is servicing requests), so we
    #     can answer the watcher's verification call-back — registering before the
    #     loop races and the entry silently never appears.
    #   * It must NOT register twice for the same owner: a duplicate
    #     RegisterStatusNotifierItem makes the host reset the indicator, which
    #     briefly drops its name owner and destroys it ~500ms later (icon flaps in
    #     then vanishes). The guard re-registers only after a real disappear /
    #     reappear of the watcher (login race, shell replacement).
    watcher_registered = [False]

    def _on_watcher_owner(owner):
        if owner and not watcher_registered[0]:
            watcher_registered[0] = True
            register_with_watcher()
        elif not owner:
            watcher_registered[0] = False

    bus.watch_name_owner(watcher_name, _on_watcher_owner)

    def _shutdown(*_):
        mgr.stop_all()
        loop.quit()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    loop.run()


if __name__ == "__main__":
    main()
