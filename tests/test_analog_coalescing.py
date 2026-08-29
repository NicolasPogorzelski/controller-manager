"""Regression coverage for DualSense -> Xbox analog translation.

A half-held trigger jitters continuously. If ABS_Z/ABS_RZ bypass coalescing,
those updates flush pending stick values at the raw Bluetooth report rate and
games consume stale steering after the stick is released.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    spec = importlib.util.spec_from_file_location("ctrlmgr_analog", MODULE)
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
except ModuleNotFoundError as ex:
    print(f"SKIP: runtime dependency missing ({ex.name}) - needs evdev/dbus/gi")
    sys.exit(0)

e = cm.e
fails = []


def check(condition, message):
    print(("  OK  " if condition else " FAIL ") + message)
    if not condition:
        fails.append(message)


print("Scenario A: Xbox stick translation is signed and center-stable")
check(cm.xbox_stick_value(0) == -32768, "minimum maps to -32768")
check(cm.xbox_stick_value(255) == 32767, "maximum maps to 32767")
check(all(cm.xbox_stick_value(value) == 0 for value in range(120, 136)),
      "the complete center jitter band maps to zero")
check(cm.xbox_stick_value(119) < 0 and cm.xbox_stick_value(136) > 0,
      "values outside the deadzone preserve direction")

print("Scenario B: half-held triggers share the bounded analog path")
check(cm.TRIGGER_AXES == {e.ABS_Z, e.ABS_RZ},
      "both analog triggers are classified")
check(cm.STICK_AXES | cm.TRIGGER_AXES == cm.COALESCED_AXES,
      "sticks and triggers use one coalesced report")
check(e.ABS_HAT0X not in cm.COALESCED_AXES and
      e.ABS_HAT0Y not in cm.COALESCED_AXES,
      "D-pad axes remain immediate")

print("Scenario C: a 700 Hz jitter stream cannot recreate the backlog")
source_hz = 700
next_report = 0.0
reports = 0
for sample in range(source_hz):
    now = sample / source_hz
    if now >= next_report:
        reports += 1
        next_report = now + cm.ANALOG_REPORT_INTERVAL

check(reports <= 61, f"700 source frames produce at most 61 reports (got {reports})")
check(cm.ANALOG_REPORT_INTERVAL <= 1 / 60,
      "an analog release is delivered within one 60 Hz interval")

if fails:
    print(f"\nRESULT: {len(fails)} FAILURE(S)")
    sys.exit(1)
print("\nRESULT: ALL PASS")
