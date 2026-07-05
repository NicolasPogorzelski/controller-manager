"""Regression test for the dbusmenu model: item ids must NEVER be recycled
across a structural change — the GNOME appindicator host caches item
properties per id and does not refresh them when a layout change reuses an id
for a structurally different item (separator→radio after a controller
connects), leaving menu entries stuck disabled. Display-only changes (radio
checkmark, labels) must instead KEEP their ids and go out as
ItemsPropertiesUpdated deltas.

No bus in the test: dbus.service.Object.__init__ is bypassed and the two
signal emitters are replaced by recording stubs (instance attributes shadow
the class methods). Skips (exit 0) when the daemon's runtime deps are
unavailable, mirroring test_reconcile.py."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    spec = importlib.util.spec_from_file_location("ctrlmgr_menu", MODULE)
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
except ModuleNotFoundError as ex:
    print(f"SKIP: runtime dependency missing ({ex.name}) — needs evdev/dbus/gi")
    sys.exit(0)

fails = []
def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)

class Pad:
    def __init__(self, ident, name="DualSense", family="ps5", mode="ps5-native"):
        self.ident = ident; self.name = name
        self.family = family; self.mode = mode

class FakeMgr:
    def __init__(self):
        self.instances = []
        self.set_mode_calls = []
    def get_instances(self):
        return list(self.instances)
    def set_mode(self, ident, mode):
        self.set_mode_calls.append((ident, mode))

def make_menu(mgr):
    menu = object.__new__(cm.DbusmenuServer)  # no bus: skip dbus Object init
    menu._mgr = mgr; menu._on_quit = lambda: None
    menu._revision = 1
    menu._items = []; menu._lookup = {}; menu._sig = None; menu._next_id = 1
    menu.layout_updates = []; menu.prop_updates = []
    menu.LayoutUpdated = (
        lambda rev, parent: menu.layout_updates.append(int(rev)))
    menu.ItemsPropertiesUpdated = (
        lambda updated, removed: menu.prop_updates.append(
            [(int(i), {str(k): v for k, v in p.items()}) for i, p in updated]))
    return menu

def ids_of(menu):
    return [id_ for id_, _ in menu._items]

QUIT = cm.QUIT_ID

# ── empty state, then a controller connects: ids must not be recycled ───────
print("Scenario A: connect after empty state -> all fresh ids")
mgr  = FakeMgr()
menu = make_menu(mgr)
menu.notify_update()
empty_ids = set(ids_of(menu)) - {QUIT}
check(len(menu._items) > 0,          "empty state produces a model")
check(not menu._lookup,              "empty state has no clickable modes")

mgr.instances = [Pad("MAC-A")]
menu.notify_update()
conn_ids = set(ids_of(menu)) - {QUIT}
check(not (empty_ids & conn_ids),
      "structural change reuses NO id (host would keep stale props)")
check(len(menu.layout_updates) == 2, "each structural change bumps LayoutUpdated")
check(len(menu._lookup) == 2,        "two clickable mode items for a ps5 pad")

# ── mode toggle: ids stable, checkmark travels as a property delta ──────────
print("Scenario B: toggle change keeps ids and emits ItemsPropertiesUpdated")
mgr.instances[0].mode = "ps5-xbox"
menu.notify_update()
check(set(ids_of(menu)) - {QUIT} == conn_ids,
      "display-only change keeps every id")
check(len(menu.layout_updates) == 2, "no LayoutUpdated for a display-only change")
check(len(menu.prop_updates) == 1,   "one ItemsPropertiesUpdated batch")
delta_ids = {i for i, _ in menu.prop_updates[0]}
radio_ids = set(menu._lookup)
check(delta_ids == radio_ids,
      "exactly the two radio items changed (toggle-state 0<->1)")

# ── disconnect / reconnect churn: every structure gets fresh ids ────────────
print("Scenario C: disconnect/reconnect churn never recycles ids")
mgr.instances = []
menu.notify_update()
gone_ids = set(ids_of(menu)) - {QUIT}
mgr.instances = [Pad("MAC-A", mode="ps5-xbox")]
menu.notify_update()
back_ids = set(ids_of(menu)) - {QUIT}
check(not (gone_ids & conn_ids) and not (back_ids & gone_ids)
      and not (back_ids & conn_ids),
      "ids from all three structures are pairwise disjoint")

# ── click routing: current ids route, stale ids are ignored ─────────────────
print("Scenario D: clicks route via the cached model")
radio_id = next(iter(menu._lookup))
menu.Event(radio_id, "clicked", 0, 0)
check(mgr.set_mode_calls == [menu._lookup[radio_id]],
      "click on a current radio id reaches set_mode")
stale_id = next(iter(conn_ids))
menu.Event(stale_id, "clicked", 0, 0)
check(len(mgr.set_mode_calls) == 1,
      "click with a stale id (old structure) is ignored, not misrouted")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
