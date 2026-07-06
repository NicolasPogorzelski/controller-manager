# Runbook: Verify Multi-Controller Operation

Confirm that four simultaneously connected pads (2× DualSense + 2× Xbox) are managed
independently: separate tray entries with "DualSense 1 / 2" numbering, per-pad modes and
lightbar colours, per-pad hidraw gating, and reconnect churn on one pad leaving the other
three untouched. The single-pad path is already verified ([verify-remapping](verify-remapping.md));
this runbook covers everything that only multiple pads can exercise.

## Preconditions

- The service is running (`systemctl --user is-active controller-manager.service`).
- Two DualSense (BT or USB) and two Xbox pads connected. Fewer pads still cover part of
  the matrix — the numbering and independence checks need at least the two DualSense.
- `lsof` available.
- If one of the Xbox pads is a **wired Xbox 360 pad**: the daemon must include the
  uinput-phys scan filter (commit that added `tests/test_multi_pad.py`); older builds
  ignore real X360 pads because xpad names them identically to our virtual output pad.

## Steps

### 1. Connect all pads and identify their nodes

Connect all four pads, then run the identification script from
[verify-remapping, step 1](verify-remapping.md) — it prints one line per adopted pad.
Note each pad's `uniq` (BT MAC / serial; empty for USB Xbox pads on xpad) — that is the
identity the tray, config and gate key on.

### 2. Check the tray menu

Open the tray menu and compare against the connected fleet.

### 3. Set independent modes

- DualSense 1 → **DualSense mode** (native, resting blue).
- DualSense 2 → **Emulate Xbox** (green).
- Both Xbox pads stay **Xbox mode**.

Re-run the identification script afterwards — DualSense 2's gate transition renumbered
its nodes; use the fresh numbers below.

### 4. Churn one pad

Power-cycle DualSense 2 (or walk it out of BT range for >10 s), reconnect it, wait two
poll ticks (~5 s).

## Verification

**Menu is complete and numbered** (after step 2):

- Four headers, one per pad. The two DualSense read "DualSense 1" and "DualSense 2";
  the Xbox pads show their model names (numbered only if both are the same model).
- Each DualSense header offers two radio modes, each Xbox header one.

**Modes and colours are independent** (after step 3):

- DualSense 1 lightbar blue, DualSense 2 lightbar green — at the same time.
- Exactly ONE virtual pad exists (DualSense 2's), the other three pads are their
  physical selves:

```bash
python3 -c "import evdev; print(*[evdev.InputDevice(p).name for p in evdev.list_devices()], sep='\n')"
# exactly one 'Microsoft X-Box 360 pad' with no physical X360 connected;
# two if one of the Xbox pads IS a real wired X360 pad
```

- Only DualSense 2's hidraw is gated (`ls -l /dev/hidrawN`: `000`, no ACL `+`);
  DualSense 1 and both Xbox pads stay open. Xbox pads on xpad have no hidraw node at
  all — nothing to check there.
- `~/.config/controller-modes.json` holds one key per configured pad (`uniq`), not a
  shared per-model entry, plus the assigned numbers under `_players`.
- White player LEDs match the menu numbers — DualSense 1 shows one lit LED, DualSense 2
  two — and stay put through repeated mode switches on either pad (Steam may flash its
  own count for a few seconds after a switch; the daemon re-asserts within ~8 s; see
  [player-leds](../docs/decisions/player-leds.md)).

**Churn is confined** (after step 4):

- DualSense 2 comes back green and remapped (virtual pad present, hidraw gated) without
  any tray interaction.
- DualSense 1 stayed blue throughout; both Xbox pads never dropped from the menu.
- All menu items still clickable: switch DualSense 1 → Emulate Xbox and back — both
  clicks take effect (guards the id-recycling regression, `tests/test_menu_ids.py`).

**Input reaches applications per pad** — with a gamepad tester (or `evtest`), press
buttons on each pad in turn: each event stream stays on its own device, DualSense 2's
events arrive under the virtual X-Box identity with X/Y correct (top button = Y).

## Failure modes

| Observation | Likely cause |
|---|---|
| Only one DualSense in the menu | pads share an identity — check `uniq` in step 1; two uniq-less pads on the SAME phys cannot be told apart |
| Wired Xbox 360 pad never appears | old build: name-based virtual filter swallows real X360 pads — update to the uinput-phys filter |
| Both DualSense switch colour together | modes keyed per model instead of per pad — config regression, check `controller-modes.json` keys |
| Wrong pad reacts to a menu click | click routed by position instead of ident — capture with `dbus-monitor` and file an issue |
| Player LEDs rotate with mode switches | daemon re-assert missing (old build) — Steam rewrites its slot count on every rebind; update to the `_players` build |
| One pad's lightbar never lights in ANY mode, but its player LEDs follow the daemon | lightbar defect on that unit — player LEDs share report and transport, so the write path is proven; cross-check the pad's own firmware-driven animations (pairing pulse, charging pulse) with no host involved |
| Menu items dead after churn | the id-recycling bug class ([tray-menu-model](../docs/decisions/tray-menu-model.md)) — capture `dbus-monitor` output and reopen |
| Second identical pad collapses into the first (USB) | both report empty `uniq` AND identical `phys` — should not happen on distinct ports; file an issue with the step-1 output |

## Rollback

Set every pad back to its native mode in the tray, or restart the service to restore all
nodes:

```bash
systemctl --user restart controller-manager.service
```
