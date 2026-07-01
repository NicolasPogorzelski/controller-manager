"""Regression test for ControllerManager._poll reconcile logic: identity keying,
rebind-on-reconnect and the debounce grace window. These guard the property that
a Bluetooth pad re-appearing on a *new* evdev node after a reconnect is rebound
in place (no tray-menu churn) rather than removed and re-added.

Stubs ControllerInstance and _scan, so no hardware / D-Bus / sudo is touched.
Skips (exit 0) if the daemon's runtime deps are unavailable, mirroring how
validate-repo.sh skips shellcheck when it is not installed."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    spec = importlib.util.spec_from_file_location("ctrlmgr", MODULE)
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
except ModuleNotFoundError as ex:
    print(f"SKIP: runtime dependency missing ({ex.name}) — needs evdev/dbus/gi")
    sys.exit(0)

# Controllable monotonic clock so the grace window is tested without real waits.
clock = [1000.0]
cm.time.monotonic = lambda: clock[0]

# Lightweight stand-in for ControllerInstance: records what the reconcile did.
class FakeInst:
    def __init__(self, path, name, vendor, product, family, mode, uniq, hidraw):
        self.path = path; self.name = name; self.vendor = vendor
        self.product = product; self.family = family; self.mode = mode
        self.uniq = uniq; self.hidraw = hidraw
        self.ident = uniq or f"{vendor:04x}:{product:04x}"
        self._gone_since = None
        self.rebinds = 0; self.stopped = False
    def apply_mode(self): pass
    def rebind(self, path, name, hidraw):
        self.rebinds += 1; self.path = path; self.name = name; self.hidraw = hidraw
    def stop(self): self.stopped = True
    def virtual_path(self): return None

cm.ControllerInstance = FakeInst

def make_mgr(scans):
    mgr = cm.ControllerManager(on_change_cb=lambda: None)
    it = iter(scans)
    mgr._scan = lambda: next(it)
    return mgr

def dev(path, uniq="ac:36:1b:70:70:e8"):
    return {"path": path, "name": "DualSense", "vendor": 0x054c,
            "product": 0x0ce6, "family": "ps5", "uniq": uniq, "hidraw": []}

fails = []
def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)

# ── Scenario A: connect → steady → RECONNECT on a new node ──────────────────
print("Scenario A: reconnect must rebind in place (no menu change)")
mgr = make_mgr([[dev("event25")], [dev("event25")], [dev("event30")]])
c1 = mgr._poll()
inst = next(iter(mgr._instances.values()))
check(c1 is True,               "connect -> changed=True (menu rebuild)")
check(len(mgr._instances) == 1, "one instance after connect")
check(mgr._poll() is False,     "steady state -> changed=False")
check(mgr._poll() is False,     "reconnect on new node -> changed=False (NO menu churn)")
check(inst.rebinds == 1,        "reconnect -> rebind() called exactly once")
check(inst.path == "event30",   "instance now bound to the new node")
check(len(mgr._instances) == 1 and inst.stopped is False,
      "same instance kept (never stopped/recreated)")

# ── Scenario B: brief disconnect inside grace, then back ────────────────────
print("Scenario B: blip shorter than grace keeps the instance")
mgr = make_mgr([[dev("event25")], [], [dev("event30")]])
mgr._poll()
inst = next(iter(mgr._instances.values()))
clock[0] += 2.0
check(mgr._poll() is False,     "absent <grace -> changed=False (kept)")
check(len(mgr._instances) == 1, "instance retained during blip")
clock[0] += 1.0
check(mgr._poll() is False,     "return within grace -> changed=False")
check(inst.rebinds == 1,        "return -> rebind() once")

# ── Scenario C: real disconnect longer than grace → removed ────────────────
print("Scenario C: absence beyond grace removes the instance")
mgr = make_mgr([[dev("event25")], [], []])
mgr._poll()
inst = next(iter(mgr._instances.values()))
clock[0] += 1.0
mgr._poll()
clock[0] += cm.REMOVE_GRACE + 0.1
check(mgr._poll() is True,      "absent >grace -> changed=True (menu rebuild)")
check(len(mgr._instances) == 0, "instance removed")
check(inst.stopped is True,     "removed instance was stop()'d")

# ── Scenario D: a genuinely different controller is a real change ──────────
print("Scenario D: a different pad (new identity) is a structural change")
mgr = make_mgr([[dev("event25", uniq="AA:AA")],
                [dev("event25", uniq="AA:AA"), dev("event40", uniq="BB:BB")]])
mgr._poll()
check(mgr._poll() is True,      "new distinct controller -> changed=True")
check(len(mgr._instances) == 2, "both controllers tracked")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
