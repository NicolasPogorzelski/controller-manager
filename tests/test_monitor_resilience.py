"""Regression test for the daemon-freeze class (field bug, 2026-07-15): during
a disconnect/reconnect of all pads, _refresh_nodes dropped a pad's path to None
and the resting-colour watch then called hidraw_for_event(None), which raised
TypeError('expected str … not NoneType'). _monitor had no guard around _poll,
so that one exception killed the monitor thread for good — hotplug, removal and
LED updates all stopped silently while the D-Bus main loop kept answering menu
clicks (modes still switched, nothing else ever updated).

Guards:
  * hidraw_for_event tolerates a None/empty path (returns []), like
    led_indicator_for_event, so a stale path is a no-op not a crash;
  * a ps5-native instance whose path went None survives _apply_led /
    watch_holders end to end;
  * _monitor keeps polling after a _poll() raises, instead of dying.

Loads the real module (no stubs on the function under test). Skips (exit 0)
when the daemon's runtime deps are unavailable, mirroring test_reconcile.py."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    spec = importlib.util.spec_from_file_location("ctrlmgr_resilience", MODULE)
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

# ── the direct guard: the real hidraw_for_event tolerates a gone path ────────
print("Scenario A: hidraw_for_event(None/'') returns [] instead of raising")
check(cm.hidraw_for_event(None) == [], "None path -> [] (was: TypeError)")
check(cm.hidraw_for_event("") == [], "empty path -> []")

# ── the instance path: a native pad dropped to path=None must not crash ─────
print("Scenario B: a ps5-native instance with path=None survives the watch")
cm.led_set_raw = lambda *a, **k: None       # never touch sudo/hidraw
cm.led_set_player = lambda *a, **k: None
cm.hidraw_gate = lambda *a, **k: None
inst = cm.ControllerInstance(None, "DualSense", 0x054c, 0x0ce6, "ps5",
                             "ps5-native", "AA:AA", "", [])
raised = None
try:
    inst._apply_led()          # _refresh_nodes can leave path None before this
    inst.watch_holders()       # resting-colour tick that used to crash
except Exception as ex:
    raised = ex
check(raised is None,
      f"path=None apply/watch did not raise (got {raised!r})")

# ── the resilience: one raising _poll must not kill the monitor loop ────────
print("Scenario C: _monitor keeps polling after a _poll() raises")
cm.traceback.print_exc = lambda: None        # keep the test output clean
mgr = cm.ControllerManager(on_change_cb=lambda: None)
poll_calls = [0]
def boom():
    poll_calls[0] += 1
    raise RuntimeError("simulated transient poll failure")
mgr._poll = boom
# time.sleep breaks the otherwise-infinite loop after a few ticks; the break
# comes from sleep (outside the try), proving the try/except around _poll is
# what kept the loop alive through each raise.
ticks = [0]
def fake_sleep(_):
    ticks[0] += 1
    if ticks[0] > 3:
        raise SystemExit
cm.time.sleep = fake_sleep
try:
    mgr._monitor()
except SystemExit:
    pass
check(poll_calls[0] >= 3,
      f"monitor called _poll across ticks despite each raising ({poll_calls[0]}x)")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
