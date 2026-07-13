# Decision: Coexistence with Steam Input (and other raw-HID consumers)

## Context

Steam Input opens every controller's `/dev/hidraw*` node the moment it connects — even
with no game running, minimized to the tray. Through that handle it reads input and
writes output reports; on a DualSense it routinely sets the lightbar-setup **"light
out"** flag, a state that lives in the pad's firmware. A kernel **LED-class** write then
changes nothing while that latch holds (the sysfs node updates, the hardware stays dark)
— so the daemon drives the lightbar with a **raw HID colour report** instead
(`controller-led lightbar-raw`, report 0x31/CRC over BT, 0x02 over USB): a raw colour
report both sets the colour *and* clears the latch, so the status colour reaches the
hardware even after Steam has latched the pad, without depending on a driver re-probe.
(Field note 2026-07-13: on kernel 7.0.14 a driver rebind did **not** clear the latch —
only a raw colour report or a pad power-cycle did; the raw path removes that dependency.)
Games with native DualSense support (SDL hidapi — most modern titles, under Steam,
Lutris or bare Wine/Proton alike) behave the same way while they run.

So there are two legitimate writers for one lightbar, and "the daemon's status colour"
and "the game's lightbar effects" cannot both win at the same time.

## Ownership model per mode

| Mode | Raw HID (hidraw) | Lightbar |
|---|---|---|
| `ps5-xbox` (remap) | daemon exclusive — the [gate](hidraw-gate.md) revokes and blocks everyone else | **green, guaranteed** — in games too (they only ever see the virtual Xbox pad) |
| `ps5-native` | applications (Steam client permanently, games while they run) — full native features | **resting blue while no game runs**; a running game's for exactly as long as it runs |

## Decision: the resting colour belongs to the daemon, the game colour to the game

The pivot (2026-07-06): with PlayStation controller support enabled the Steam client
holds every DualSense **permanently**, so "someone holds the pad" no longer separates
an idle desktop from a running game. The policy therefore *classifies* instead of
counting holders. Two cheap signals decide who owns the lightbar each monitor tick:

- **A Steam-launched title is alive.** Every Steam launch — native or Proton — goes
  through the client's wrapper chain with `SteamLaunch AppId=<id>` on the command line
  (`steam-launch-wrapper` and `reaper` both carry it and live exactly as long as the
  game), so one `/proc` cmdline sweep answers "is a game running" without any Steam
  IPC. Needed because a game under Steam Input never opens the pad itself — the
  client's permanent hold is all the holder scan sees.
- **A foreign holder exists** — any raw-HID holder that is not the Steam client
  (`comm` ≠ `steam`). Direct-access titles (Steam Input disabled per game, Lutris /
  bare Wine, emulators) open the pad in their own name.

While either signal is true the lightbar is legitimately the game's: hands off, cancel
pending repaints. Otherwise the mode colour is the daemon's — the idle client paints
its slot colours only on discrete events (startup, pad adoption, game exit) and stays
quiet in between, so the daemon re-asserts the resting colour after each of them.
Rewriting an unchanged colour through the kernel LED class is visually a no-op, so
this is *not* the flickering paint war that per-tick enforcement against a running
game's effects would be (that remains rejected, see below):

- **a raw fd closed** (direct-access game exited, Steam quit): *rearm* (driver rebind,
  which revokes any lingering fd) and repaint. The repaint itself now clears any "light
  out" latch (raw colour report), so recovery no longer hinges on the rebind; the rebind
  is still deferred while a game is active — it would yank running fds;
- **a Steam Input game exited without any fd closing**: the client restores its slot
  colour on the way out → one repaint ~3 s later gets the last word, no rearm needed;
- **the client (re)opened the pad** (enumeration after one of our rebinds, or a
  client start): it writes its defaults once and goes quiet → one repaint ~6 s later
  outlasts it (field-verified: Steam reopened ~6 s after a rebind and stomped the
  freshly painted colour);
- **backstop**: a slow periodic re-assert (~15 s after whatever painted last)
  recovers the colour from anything unforeseen — the pad shows its mode at a glance
  at all times outside a running game.

Games that never touch raw HID (plain evdev/joystick titles) never take the lightbar,
so the resting colour simply stays. Regression coverage:
`tests/test_steam_ownership.py`.

## Required setup in the Steam client

**Steam → Settings → Controller → PlayStation controller support:
"Enabled in Games w/o Support"** (`SteamController_PSSupport "1"` in
`userdata/<id>/config/localconfig.vdf`).

- Titles **with** native DualSense support talk to the pad directly — adaptive
  triggers, gyro, haptics, everything the title implements.
- Titles **without** native support get Steam Input's remapping (gyro-as-mouse etc.)
  via the client's virtual pad.
- The third setting, "Enabled" (`2`), would put Steam Input in front of *every*
  title, masking native DualSense features behind the client's virtual pad — not
  what this project wants as a default; it remains available per game via the
  title's controller settings.

**Steam → Settings → Controller → Xbox controller support: off**
(`SteamController_XBoxSupport "0"`) — unchanged and still required: Steam Input's
support toggles are per-VID/PID family and blind to a device being `uinput`-virtual.
With Xbox support on, Steam swallows `ps5-xbox`'s virtual pad without passing a
working controller to the game (field-confirmed 2026-07-05, *Secrets of Grindea*:
the virtual pad has no hidraw node, which Steam's Xbox path needs for negotiation —
input was dead in-game; disabling the toggle fixed it immediately). See
[output-protocol-constraints.md](output-protocol-constraints.md).

## History: the SDL ignore-vars workaround (2026-07-06, superseded same day)

An earlier field finding, kept for the record: with **both** support toggles off the
client *still* opens every DualSense hidraw at startup for identification
("Controller using HIDAPI driver" in `logs/controller.txt`, build 1782866176),
repaints the pads with its slot colours and leaves the "light out" latch behind on
exit. `"controller_blacklist" "054c/0ce6"` in `config/config.vdf` — the classic
remedy — was demonstrably loaded yet **ineffective**. What did work was scoping the
SDL ignore list to the Steam process via a user-local `steam.desktop` override:

```
SDL_GAMECONTROLLER_IGNORE_DEVICES=0x054c/0x0ce6
SDL_HIDAPI_IGNORE_DEVICES=0x054c/0x0ce6
```

That gave the daemon sole lightbar ownership — at the price of Steam-launched titles
inheriting the variables and losing native access to the physical DualSense entirely.
It was the "Steam sees nothing" end of the spectrum; the project has since moved to
the other clean end, "Steam manages fully" (above), which needs neither the override
nor the (useless) blacklist entry. Both have been removed from the deployed setup.
The middle ground — toggles off but no ignore vars — remains the one broken state:
the identification open holds and latches pads for no benefit.

## Rejected alternatives

- **Enforcing blue per-tick against a running game** — visible flicker war against
  game effects; breaks the features native mode exists for. The periodic backstop
  above never fires while a game owns the pad.
- **Reading the current lightbar colour to decide** — impossible: raw-HID writes by
  other processes do not go through the kernel LED class, so there is nothing to
  read back.
- **Keeping the SDL ignore-vars override** (see History) — daemon-guaranteed colours
  at all times, but Steam-launched titles lose every native DualSense feature; the
  per-game escape hatch (`env -u … %command%`) made full-featured gaming opt-in
  fiddling instead of the default.
