# Decision: Coexistence with Steam Input (and other raw-HID consumers)

## Context

Steam Input opens every controller's `/dev/hidraw*` node the moment it connects — even
with no game running, minimized to the tray. Through that handle it reads input and
writes output reports; on a DualSense it routinely sets the lightbar-setup **"light
out"** flag, a state that lives in the pad's firmware: from then on plain colour writes
(including the kernel LED-class writes this daemon uses) change nothing until the driver
re-probes the pad. Games with native DualSense support (SDL hidapi — most modern titles,
under Steam, Lutris or bare Wine/Proton alike) behave the same way while they run.

So there are two legitimate writers for one lightbar, and "the daemon's status colour"
and "the game's lightbar effects" cannot both win at the same time.

## Ownership model per mode

| Mode | Raw HID (hidraw) | Lightbar |
|---|---|---|
| `ps5-xbox` (remap) | daemon exclusive — the [gate](hidraw-gate.md) revokes and blocks everyone else | **green, guaranteed** |
| `ps5-native` | applications (Steam, games) — full native features, daemon hands off | **resting blue**, games may override while they hold the pad |

## Decision: resting colour, not a paint war

In `ps5-native` the daemon must not fight applications for the lightbar (a periodic
repaint would flicker against game effects and defeat native support). Instead it watches
**who holds the pad's hidraw nodes** (`/proc/*/fd` — same-user processes, which is
exactly the Steam/games population):

- someone holds the pad → the lightbar is legitimately theirs; hands off;
- the **last holder disappears** (game exited, Steam closed) → the pad may be latched
  dark, so the daemon *rearms* it (driver rebind → fresh lightbar setup) and repaints the
  resting blue within a couple of monitor ticks;
- the pad is **already held when the daemon adopts it** (field case: a freshly paired
  second DualSense — Steam grabbed it at connect and latched it before we ever saw it) →
  same problem, but no holder ever *disappears* and, the identity being new to the gate,
  the adoption `restore` is no marker transition and rebinds nothing. Adoption therefore
  forces a one-time rearm when foreign holders pre-date it. Outside adoption a
  native-mode `apply_mode` never rearms — that would yank a running game's fd.

Every `apply_mode` additionally arms a **one-shot delayed repaint** (~6 s): the colour
write immediately after a driver rebind races the pad's BT re-probe and can be dropped —
the re-assert is what makes the colour stick. It fires in remap modes too, where the
holder policy itself is inactive.

Games that never touch raw HID (plain evdev/joystick titles) never take the lightbar, so
the resting blue simply stays.

## Required setup: disable PlayStation *and* Xbox controller support in the Steam client

While the Steam *client* runs with PlayStation controller support enabled, it holds the
pad permanently — indistinguishable from a running game, so the resting colour can never
return until Steam exits. The supported setup is therefore:

**Steam → Settings → Controller → PlayStation controller support: off.**

Consequences of that setting:

- The Steam client leaves the physical DualSense alone; the resting blue works.
- Games keep their **native** DualSense features — modern titles talk to the pad through
  their own SDL/hidapi, not through the Steam client.
- What is lost is Steam-Input *remapping of the physical DualSense identity* — which this
  project replaces anyway: switch the pad to `ps5-xbox` and Steam Input sees a plain
  Xbox 360 pad it can remap freely.

Without the setting, everything still degrades gracefully: green in `ps5-xbox` stays
guaranteed (the gate revokes Steam), and blue returns whenever Steam fully exits.

Steam Input's controller support is per-VID/PID family, not per-application, and it does
not care whether a device is physical or `uinput`-virtual: enabling **Xbox controller
support** makes Steam hold `ps5-xbox`'s virtual Xbox pad the same way PlayStation support
holds the physical DualSense. Confirmed in the field (2026-07-06, Fedora/GNOME notebook,
*Secrets of Grindea* under Steam): with Xbox controller support on, Steam opened the
virtual pad's evdev node (visible in `lsof`) but never passed a working controller through
to the game — input was dead in-game even though the daemon's remap was independently
verified correct at the event level. The virtual pad has no hidraw node (see
[output-protocol-constraints.md](output-protocol-constraints.md)), which Steam's Xbox
input path appears to need for full negotiation; lacking it, Steam swallows the device
without emulating it. Disabling **Xbox controller support** fixed it immediately, no
daemon-side change needed.

**Steam → Settings → Controller → Xbox controller support: off**, in addition to the
PlayStation toggle above, is therefore required for `ps5-xbox` to reach games running
under Steam.

## Field finding (2026-07-06): the toggles do not stop the identification open

Both support toggles off is necessary but **not sufficient**. The Steam client still
opens every DualSense hidraw node at startup for identification ("Controller using
HIDAPI driver" in `logs/controller.txt`), holds it, and paints/latches the lightbar —
verified with client build 1782866176: with Steam stopped both pads held the daemon's
resting blue; starting Steam took both nodes within a second and repainted the pads with
Steam's own slot colours despite `SteamController_PSSupport=0` and
`SteamController_XBoxSupport=0`. On exit that open leaves the "light out" latch behind.

- `"controller_blacklist" "054c/0ce6"` in `config/config.vdf` — the classic remedy —
  was **ineffective**: the entry survived the restart and was demonstrably loaded, yet
  the client still ran its HIDAPI driver on both pads.
- What works: the SDL ignore-list environment variables, set for the Steam process only:

  ```
  SDL_GAMECONTROLLER_IGNORE_DEVICES=0x054c/0x0ce6
  SDL_HIDAPI_IGNORE_DEVICES=0x054c/0x0ce6
  ```

  With these, Steam opens no DualSense hidraw node at all and `controller.txt` shows no
  HIDAPI driver line. Deployed as a user-local desktop-file override
  (`~/.local/share/applications/steam.desktop`, every `Exec=` wrapped in `env …`), which
  shadows the system entry for GUI launches without touching other SDL applications —
  a session-wide export would blind *every* SDL game to the physical pad and break
  `ps5-native` outside Steam.

Trade-off: games **launched by Steam** inherit the variables, so Steam-launched titles
lose native access to the *physical* DualSense identity too. That matches the ownership
model (Steam is served by `ps5-xbox`'s virtual pad); a per-game escape hatch exists via
launch options: `env -u SDL_GAMECONTROLLER_IGNORE_DEVICES -u SDL_HIDAPI_IGNORE_DEVICES
%command%`. Steam started from a terminal bypasses the desktop override; the daemon's
rearm-on-last-holder-exit then heals the latch as before.

## Rejected alternatives

- **Enforcing blue periodically in native mode** — visible flicker war against Steam and
  game effects; breaks the features native mode exists for.
- **Reading the current lightbar colour to decide** — impossible: raw-HID writes by other
  processes do not go through the kernel LED class, so there is nothing to read back.
