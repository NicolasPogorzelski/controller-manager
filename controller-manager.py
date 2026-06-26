#!/usr/bin/env python3
"""
Controller Manager — unified tray + per-controller remapping.

Supports:
  PS5 DualSense  →  nativ  |  als Xbox 360 ausgeben
  Xbox Controller →  nativ  |  als PS5 ausgeben
Multiple controllers simultaneously, each with its own mode.
"""

import os, sys, json, signal, threading, time, subprocess, glob
import evdev
from evdev import UInput, ecodes as e
import dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

# ── Konstanten ───────────────────────────────────────────────────────────────

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

MODE_LABELS = {
    "ps5-native":  "PS5 nativ",
    "ps5-xbox":    "Als Xbox ausgeben",
    "xbox-native": "Xbox nativ",
    "xbox-ps5":    "Als PS5 ausgeben",
}

MODES_FOR_FAMILY = {
    "ps5":  ["ps5-native",  "ps5-xbox"],
    "xbox": ["xbox-native", "xbox-ps5"],
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
# An evdev grab (EVIOCGRAB) only blocks the evdev node. Wine/Proton's winebus
# reads game controllers straight from /dev/hidraw*, so a remapped DualSense
# would still reach the game as a PS5 pad — in parallel with our virtual Xbox
# pad — causing double input. We therefore chmod the physical controller's
# hidraw node to 000 for the duration of any remap. This is system-wide and
# launcher-agnostic: it hides the pad from Steam, Lutris, umu and native games
# alike. The chmod needs root, delegated to a tightly-scoped helper via a
# NOPASSWD sudoers rule. If the helper/rule is absent, this is a silent no-op.
#
# The gate operates on a SPECIFIC hidraw node (not a VID:PID), so two identical
# controllers (same VID:PID, different physical device) can be gated indepen-
# dently — only the remapped one is hidden.

GATE_BIN = "/usr/local/bin/controller-hidraw-gate"

def hidraw_for_event(ev_path):
    """Return /dev/hidrawN belonging to the same HID device as an evdev node,
    or None (e.g. xpad Xbox pads expose no hidraw)."""
    base = f"/sys/class/input/{os.path.basename(ev_path)}/device"
    if not os.path.exists(base):
        return None
    hid_dir = os.path.realpath(os.path.join(base, "..", ".."))
    nodes = glob.glob(os.path.join(hid_dir, "hidraw", "hidraw*"))
    return f"/dev/{os.path.basename(nodes[0])}" if nodes else None

def hidraw_gate(action, node):
    """action: 'block' | 'restore'; node: /dev/hidrawN or None.
    No-op if there is no hidraw node, or the helper/sudo rule is missing."""
    if not node or not os.path.exists(GATE_BIN):
        return
    try:
        subprocess.run(
            ["sudo", "-n", GATE_BIN, action, node],
            timeout=5, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as ex:
        print(f"hidraw_gate: {action} {node} failed: {ex}", file=sys.stderr)


# ── Remapper thread ──────────────────────────────────────────────────────────

class Remapper(threading.Thread):
    """Grabs a source controller and emits a virtual target device."""

    def __init__(self, src_path, target_spec):
        super().__init__(daemon=True)
        self._src_path   = src_path
        self._target     = target_spec
        self._stop_event = threading.Event()
        self._ui         = None
        self._virtual_path = None
        self._src_dev    = None

    @property
    def virtual_path(self):
        return self._virtual_path

    def stop(self):
        self._stop_event.set()
        # Close the fd so read_loop() unblocks immediately instead of waiting for an event
        dev = self._src_dev
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass

    def run(self):
        try:
            src = evdev.InputDevice(self._src_path)
        except Exception as ex:
            print(f"remapper: cannot open {self._src_path}: {ex}", file=sys.stderr)
            return
        self._src_dev = src

        caps = src.capabilities()
        caps.pop(e.EV_SYN, None)
        caps.pop(e.EV_FF,  None)

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

        try:
            for event in src.read_loop():
                if self._stop_event.is_set():
                    break
                if event.type in (e.EV_KEY, e.EV_ABS, e.EV_REL):
                    ui.write(event.type, event.code, event.value)
                    ui.syn()
        except OSError:
            pass
        finally:
            try: src.ungrab()
            except Exception: pass
            try: ui.close()
            except Exception: pass
            self._virtual_path = None
            print(f"remapper: stopped for {self._src_path}", file=sys.stderr)


# ── Controller instance ──────────────────────────────────────────────────────

class ControllerInstance:
    def __init__(self, path, name, vendor, product, family, mode, uniq, hidraw):
        self.path    = path
        self.name    = name
        self.vendor  = vendor
        self.product = product
        self.family  = family
        self.mode    = mode
        self.uniq    = uniq       # stable per-device id (BT MAC / serial)
        self.hidraw  = hidraw     # /dev/hidrawN or None
        self._remap  = None

    def display_name(self):
        # Disambiguate identical controllers (e.g. two DualSense) by a short
        # tail of their unique id, so the tray shows which physical pad is which.
        if self.uniq:
            tail = self.uniq.replace(":", "")[-4:]
            return f"{self.name} ({tail})"
        return self.name

    def _target_for_mode(self):
        if self.mode == "ps5-xbox":
            return VIRTUAL_XBOX
        if self.mode == "xbox-ps5":
            return VIRTUAL_PS5
        return None

    def apply_mode(self):
        """Stop existing remapper, start new one if needed."""
        if self._remap:
            self._remap.stop()
            self._remap = None

        target = self._target_for_mode()
        if target:
            # Hide this pad's raw HID node from Wine/Proton before the remapper
            # takes over (the evdev grab alone does not cover hidraw).
            hidraw_gate("block", self.hidraw)
            r = Remapper(self.path, target)
            r.start()
            self._remap = r
        else:
            # Native mode: make sure raw HID access is open again.
            hidraw_gate("restore", self.hidraw)

    def virtual_path(self):
        return self._remap.virtual_path if self._remap else None

    def stop(self):
        if self._remap:
            self._remap.stop()
            self._remap = None
        # Restore raw HID access whenever we stop managing this pad
        # (disconnect or shutdown), so a blocked node never lingers.
        hidraw_gate("restore", self.hidraw)


# ── Controller Manager ───────────────────────────────────────────────────────

class ControllerManager:
    def __init__(self, on_change_cb):
        self._lock       = threading.Lock()
        self._instances  = {}      # path → ControllerInstance
        self._config     = load_config()
        self._on_change  = on_change_cb   # called (from thread) when list changes
        self._monitor_th = threading.Thread(target=self._monitor, daemon=True)

    def start(self):
        self._monitor_th.start()

    def _virtual_paths(self):
        paths = set()
        for inst in self._instances.values():
            vp = inst.virtual_path()
            if vp:
                paths.add(vp)
        return paths

    def _scan(self):
        """Return a list of dicts describing connected real controllers."""
        result = []
        virtual = self._virtual_paths()
        for path in evdev.list_devices():
            if path in virtual:
                continue
            try:
                dev = evdev.InputDevice(path)
            except Exception:
                continue
            if dev.name in VIRTUAL_NAMES:
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
                "hidraw":  hidraw_for_event(path),
            })
        return result

    def _monitor(self):
        while True:
            found = {d["path"]: d for d in self._scan()}
            changed = False
            with self._lock:
                # Remove disconnected
                for path in list(self._instances):
                    if path not in found:
                        self._instances[path].stop()
                        del self._instances[path]
                        changed = True
                # Add new
                for path, d in found.items():
                    if path not in self._instances:
                        cfg_key = d["uniq"] or f'{d["vendor"]:04x}:{d["product"]:04x}'
                        mode = self._config.get(
                            cfg_key, DEFAULT_MODES.get(d["family"], "ps5-native"))
                        inst = ControllerInstance(
                            d["path"], d["name"], d["vendor"], d["product"],
                            d["family"], mode, d["uniq"], d["hidraw"])
                        inst.apply_mode()
                        self._instances[path] = inst
                        changed = True
            if changed:
                GLib.idle_add(self._on_change)
            time.sleep(2)

    def get_instances(self):
        with self._lock:
            return list(self._instances.values())

    def set_mode(self, path, mode):
        with self._lock:
            inst = self._instances.get(path)
            if not inst:
                return
            inst.mode = mode
            inst.apply_mode()
            cfg_key = inst.uniq or f"{inst.vendor:04x}:{inst.product:04x}"
            self._config[cfg_key] = mode
        save_config(self._config)
        GLib.idle_add(self._on_change)

    def stop_all(self):
        with self._lock:
            for inst in self._instances.values():
                inst.stop()


# ── dbusmenu ────────────────────────────────────────────────────────────────

class DbusmenuServer(dbus.service.Object):

    def __init__(self, bus, path, manager):
        dbus.service.Object.__init__(self, bus, path)
        self._mgr      = manager
        self._revision = 1

    # ── menu building ────────────────────────────────────────────────────────

    def _make_item(self, id_, props):
        return dbus.Struct(
            [dbus.Int32(id_),
             dbus.Dictionary(props, signature="sv"),
             dbus.Array([], signature="v")],
            signature="(ia{sv}av)"
        )

    def _build_items(self):
        """Flat menu: header + radio items per controller, then Quit."""
        items  = []
        id_    = 1
        instances = self._mgr.get_instances()

        for inst in instances:
            # Section header (disabled label)
            items.append(self._make_item(id_, {
                "label":   dbus.String(inst.display_name()),
                "enabled": dbus.Boolean(False),
            }))
            id_ += 1

            modes = MODES_FOR_FAMILY.get(inst.family, [])
            for mode in modes:
                items.append(self._make_item(id_, {
                    "label":        dbus.String(MODE_LABELS[mode]),
                    "enabled":      dbus.Boolean(True),
                    "toggle-type":  dbus.String("radio"),
                    "toggle-state": dbus.Int32(1 if inst.mode == mode else 0),
                    # encode controller path + mode in id via lookup below
                }))
                id_ += 1

            # Separator between controllers
            if inst is not instances[-1]:
                items.append(self._make_item(id_, {"type": dbus.String("separator")}))
                id_ += 1

        # Final separator + Quit
        items.append(self._make_item(id_, {"type": dbus.String("separator")}))
        items.append(self._make_item(QUIT_ID, {"label": dbus.String("Beenden"),
                                                "enabled": dbus.Boolean(True)}))
        return items

    def _build_lookup(self):
        """id → (controller_path, mode) for click handling."""
        lookup = {}
        id_ = 1
        for inst in self._mgr.get_instances():
            id_ += 1   # skip header
            for mode in MODES_FOR_FAMILY.get(inst.family, []):
                lookup[id_] = (inst.path, mode)
                id_ += 1
            id_ += 1   # separator
        return lookup

    def _layout(self):
        items = self._build_items()
        return dbus.Struct(
            [dbus.Int32(0),
             dbus.Dictionary({}, signature="sv"),
             dbus.Array(
                 [dbus.Struct(i, signature="(ia{sv}av)") for i in items],
                 signature="v")],
            signature="(ia{sv}av)"
        )

    def notify_update(self):
        self._revision += 1
        self.LayoutUpdated(self._revision, 0)

    # ── dbusmenu methods ─────────────────────────────────────────────────────

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parentId, recursionDepth, propertyNames):
        return dbus.UInt32(self._revision), self._layout()

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, propertyNames):
        return dbus.Array([], signature="(ia{sv})")

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="is", out_signature="v")
    def GetProperty(self, id_, name):
        return dbus.String("")

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="isvu", out_signature="")
    def Event(self, id_, eventId, data, timestamp):
        if eventId != "clicked":
            return
        if id_ == QUIT_ID:
            GLib.idle_add(lambda: (os._exit(0), False)[1])
            return
        lookup = self._build_lookup()
        if id_ in lookup:
            ctrl_path, mode = lookup[id_]
            self._mgr.set_mode(ctrl_path, mode)

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
            title = f"Controller ({len(active_remaps)} remapped)"
        else:
            icon  = "input-gaming-symbolic"
            title = "Controller (alle nativ)"
        n = len(instances)
        tip = f"{n} Controller verbunden" if n else "Kein Controller"

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
    dbus.service.BusName(BUS_NAME, bus)

    tray_ref = [None]

    def on_change():
        if tray_ref[0]:
            tray_ref[0].refresh()

    mgr  = ControllerManager(on_change)
    menu = DbusmenuServer(bus, MENU_PATH, mgr)
    tray = TrayIcon(bus, mgr, menu)
    tray_ref[0] = tray

    try:
        watcher = bus.get_object("org.kde.StatusNotifierWatcher",
                                  "/StatusNotifierWatcher")
        watcher.RegisterStatusNotifierItem(
            ITEM_PATH, dbus_interface="org.kde.StatusNotifierWatcher")
    except Exception as ex:
        print(f"controller-manager: watcher registration failed: {ex}",
              file=sys.stderr)

    mgr.start()

    def _shutdown(*_):
        mgr.stop_all()
        loop.quit()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    loop.run()


if __name__ == "__main__":
    main()
