# controller-manager

Systemweite Controller-Verwaltung für Bazzite (GNOME/Wayland): remappt
PS5-DualSense ↔ Xbox-Controller über ein permanentes Tray-Icon, damit Spiele
(Steam, Lutris/umu, native) jeden Controller im gewünschten Protokoll sehen —
unabhängig vom Launcher.

| Controller | Modus | Ergebnis |
|---|---|---|
| PS5 DualSense | PS5 nativ | roher DualSense ans System |
| PS5 DualSense | Als Xbox ausgeben | virtueller Xbox-360-Pad |
| Xbox Controller | Xbox nativ | kein Remapping |

(„Xbox → Als PS5 ausgeben" wird nicht angeboten — funktioniert in Spielen nicht, siehe `controller-setup.md` Problem 4.)

## Komponenten

| Datei | Ziel | Zweck |
|---|---|---|
| `controller-manager.py` | `~/.local/bin/` | Daemon: Tray (StatusNotifierItem + dbusmenu), evdev-Grab + UInput-Remapping, Hotplug |
| `controller-manager.service` | `~/.config/systemd/user/` | systemd-User-Service (Autostart) |
| `controller-hidraw-gate` | `/usr/local/bin/` (root) | sperrt/öffnet `/dev/hidraw*` eines remappten Pads, damit Wine/Proton (winebus) den physischen Controller nicht parallel liest |
| `controller-hidraw.sudoers` | `/etc/sudoers.d/controller-hidraw` | NOPASSWD, eng auf den Helper begrenzt |

Laufzeit-Modi liegen in `~/.config/controller-modes.json` (nicht im Repo).

## Installation

```bash
./install.sh        # deployt alles; fragt einmal nach sudo (Gate + sudoers)
systemctl --user enable controller-manager.service   # Autostart, einmalig
```

## Hintergrund / Designentscheidungen

Ausführliche Problem-/Fallstrick-Analyse (warum der evdev-Grab allein nicht
reicht, hidraw-Gate, Steam-Input-Flags, fgmod vs. bgmod, …) in
[`controller-setup.md`](controller-setup.md).
