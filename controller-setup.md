# Controller Management – Setup-Dokumentation

## Ziel

Einheitliche, systemweite Controller-Verwaltung für alle Spiele (Steam und Lutris), steuerbar über ein permanentes Tray-Icon. Unterstützte Controller und Modi:

| Controller | Modus | Ergebnis |
|---|---|---|
| PS5 DualSense | PS5 nativ | Kein Remapping, roher DualSense ans System |
| PS5 DualSense | Als Xbox ausgeben | Virtueller Xbox 360 Pad via UInput |
| Xbox Controller | Xbox nativ | Kein Remapping |
| Xbox Controller | Als PS5 ausgeben | Virtueller DualSense via UInput |

Mehrere Controller gleichzeitig werden unterstützt, jeder mit eigenem Modus.

---

## Systemumgebung

- **OS**: Bazzite (immutables Fedora-basiertes Gaming-OS)
- **Desktop**: GNOME auf Wayland
- **Launcher**: Lutris + umu-run + GE-Proton (für Windows-Spiele)
- **Testspiel**: Resident Evil Requiem (XInput-only, via Lutris/GE-Proton10-34)

---

## Problem 1: Xbox Controller – UI wechselte zwischen Controller und Tastatur

**Ursache**: Steam Input war im Desktop-Modus aktiv (`SteamController_XBoxSupport "1"`) und hat den Xbox Controller in Tastatur-/Maus-Events umgewandelt. Lutris-Spiele bekamen dadurch gemischte Eingaben.

**Fix**: Beide Controller-Support-Flags in der Steam-Konfiguration deaktiviert.

**Datei**: `~/.local/share/Steam/userdata/<STEAM_USER_ID>/config/localconfig.vdf`
```
"SteamController_PSSupport"     "0"
"SteamController_XBoxSupport"   "0"
```

> **Wichtig**: Steam muss beim Bearbeiten dieser Datei **geschlossen** sein, sonst überschreibt Steam die Änderungen beim Beenden.

> **Warum beide auf 0**: Mit `PSSupport "1"` grabbt Steam den DualSense systemweit und wandelt ihn in ein eigenes virtuelles Gerät um. Proton/Lutris-Spiele können dieses virtuelle Gerät dann nicht korrekt verarbeiten. Beide Flags auf `"0"` bedeutet: Steam lässt alle Controller in Ruhe, unser Daemon übernimmt die Kontrolle vollständig.

---

## Problem 2: PS5 DualSense wird in XInput-Only-Spielen nicht erkannt

**Ursache**: RE: Requiem unterstützt nur XInput (Xbox-Protocol). Der DualSense wird vom `hid_playstation`-Kernel-Treiber als PS5-Gerät angemeldet. XInput-Spiele ignorieren das.

**Zusatzproblem**: SDL-Umgebungsvariablen (`SDL_JOYSTICK_HIDAPI_PS5` etc.) werden vom `pressure-vessel`-Sandbox von Proton herausgefiltert und erreichen Wine nicht. `PROTON_*`-Variablen funktionieren, aber es gibt keine passende Variable für diesen Fall.

> **Korrektur (später verifiziert)**: Diese Annahme stimmt so **nicht**. Ein empirischer Test (`cmd /c set` durch den echten Stack) zeigte, dass SDL-Vars sowohl über GE-Proton-Wine direkt **als auch** über umu-run/pressure-vessel **durchkommen** und im Windows-Env ankommen. Sie blieben bei Problem 3 trotzdem wirkungslos – aber aus einem anderen Grund (winebus liest hidraw statt SDL), nicht wegen Filterung. Siehe Problem 3.

**Weiteres Problem**: bgmod (GOverlay-Komponente, als `prefix_command` in Lutris) baut die Prozessumgebung via `execvpe` explizit auf und ignoriert unbekannte Schlüssel in seiner `[Env]`-Konfig. Umgebungsvariablen können daher nicht über bgmod an Wine weitergegeben werden.

**Fix**: Systemweiter Remapper via `python-evdev` + Linux UInput. Der DualSense wird auf Kernel-Ebene exklusiv gegrabbt und als virtueller Xbox 360 Controller ausgegeben – bevor Proton oder Steam überhaupt ansetzen können.

---

## Problem 3: Doppeleingabe im „Als Xbox ausgeben"-Modus

**Symptom**: DualSense auf „Als Xbox ausgeben" gestellt → Spiel (z.B. Gothic 1 Remake, Unreal Engine) zeigt **PS5-Glyphen** statt Xbox und reagiert auf **doppelte Eingaben**. Native Modi und XInput-only-Spiele (RE: Requiem) waren nie betroffen.

**Diagnose (mit laufendem Spiel, `lsof`)**:
```
event24 (DualSense evdev)  → offen nur von python3 (unserem Manager)   ← Grab funktioniert
hidraw8 (DualSense hidraw) → offen von winedevice (winebus) + steam    ← LECK
```

**Ursache**: `evdev.grab()` (EVIOCGRAB) sperrt **nur den evdev-Knoten**. Proton/Wine liest Game-Controller aber über **`winebus` direkt aus `/dev/hidraw*`** – ein Pfad, den der evdev-Grab nicht abdeckt. Der physische DualSense (als PS5) erreichte das Spiel also weiter über hidraw, **parallel** zum virtuellen Xbox-Pad → Doppeleingabe + PS5-Erkennung.

`SDL_GAMECONTROLLER_IGNORE_DEVICES` half **nicht**: Es wirkt nur auf SDLs Enumeration; winebus liest den rohen hidraw-Knoten an SDL vorbei. (Die Variable kam nachweislich im Spiel an – sie greift nur am falschen Layer.)

**Fix**: Mode-gekoppeltes **hidraw-Gate**. Sobald ein Controller in einen Remap-Modus wechselt, wird sein `/dev/hidrawN` per `chmod 000` für *alle* Prozesse unzugänglich gemacht; bei „nativ"/Abstecken/Shutdown wieder auf `666`. Da das eine systemweite Geräte-Eigenschaft ist, wirkt es **launcher-übergreifend** (Steam, Lutris, umu, native) – ganz ohne per-Spiel-Konfiguration. Der `chmod` braucht Root und läuft über einen eng begrenzten Helper + NOPASSWD-sudoers.

### Root-Helper `/usr/local/bin/controller-hidraw-gate`

- Aufruf: `controller-hidraw-gate block|restore VID:PID`
- Akzeptiert **nur** VID:PIDs aus einer einprogrammierten Allowlist bekannter Controller und fasst **nur** `/dev/hidraw*` an → keine Privesc-Fläche.
- Findet den passenden hidraw-Knoten via `HID_ID` in `/sys/class/hidraw/*/device/uevent` (Format `BUS:0000VVVV:0000PPPP`).
- root-eigen, `0755` (nicht user-schreibbar — Voraussetzung für sicheres NOPASSWD).

### sudoers `/etc/sudoers.d/controller-hidraw` (`0440`)
```
admin ALL=(root) NOPASSWD: /usr/local/bin/controller-hidraw-gate
```

### Manager-Integration

`controller-manager.py`:
- neue Funktion `hidraw_gate(action, vendor, product)` → ruft `sudo -n controller-hidraw-gate …` (stiller No-op, falls Helper/Regel fehlen).
- `ControllerInstance.apply_mode()`: Remap-Modus → `block`, Nativ → `restore`.
- `ControllerInstance.stop()`: immer `restore` (Abstecken/Shutdown), damit nie eine Sperre hängenbleibt.

> **Wichtig**: Die Sperre muss **vor** dem Spielstart aktiv sein. Hält ein Prozess das hidraw-fd schon offen (Spiel lief vor dem Moduswechsel), schließt der spätere `chmod` dieses fd nicht – Spiel neu starten.

> **Hinweis xpad**: Xbox-Controller über den `xpad`-Treiber haben **keinen** hidraw-Knoten; dort macht das Gate korrekt nichts und der evdev-Grab allein genügt.

---

## Lösung: controller-manager.py

### Installierte Dateien

| Pfad | Zweck |
|---|---|
| `~/.local/bin/controller-manager.py` | Haupt-Daemon: Tray-Icon + Remapping + Hotplug |
| `~/.config/systemd/user/controller-manager.service` | systemd User-Service (Autostart mit GNOME-Session) |
| `~/.config/controller-modes.json` | Persistierte Modi pro Controller (`vendorid:productid → modus`) |
| `/usr/local/bin/controller-hidraw-gate` | Root-Helper: sperrt/öffnet `/dev/hidraw*` eines Controllers (siehe Problem 3) |
| `/etc/sudoers.d/controller-hidraw` | NOPASSWD-Regel, eng begrenzt auf den Helper |

### Abhängigkeiten

```bash
# Bereits auf Bazzite vorhanden:
python3-evdev    # v1.9.3
python3-dbus     # dbus-python
python3-gi       # GLib mainloop
```

Das Script benötigt außerdem Zugriff auf `/dev/uinput`. Auf Bazzite ist der aktuelle User bereits in der richtigen Gruppe.

### Service verwalten

```bash
# Autostart aktivieren (einmalig)
systemctl --user enable controller-manager.service

# Starten
systemctl --user start controller-manager.service

# Status / Logs
systemctl --user status controller-manager.service
journalctl --user -u controller-manager.service -f

# Neu starten (nach Code-Änderungen)
systemctl --user restart controller-manager.service
```

### Tray-Icon

Das Icon erscheint in der GNOME-Taskleiste (erfordert die GNOME-Extension **AppIndicator and KStatusNotifierItem Support** von `rgcjonas.gmail.com`, auf Bazzite vorinstalliert). Klick öffnet ein Menü mit allen verbundenen Controllern und deren Modi als Radio-Buttons. Das Menü aktualisiert sich automatisch wenn Controller ein- oder ausgesteckt werden (Polling alle 2 Sekunden).

---

## Architektur

```
Physischer Controller (evdev /dev/input/eventX)
        │
        ├── Modus "nativ"
        │       └── kein Eingriff, Gerät direkt für alle Programme sichtbar
        │
        └── Modus "remappen" (ps5-xbox / xbox-ps5)
                └── Remapper-Thread:
                        ├── evdev.InputDevice.grab()  → exklusiver Kernel-Zugriff
                        ├── UInput(...)               → virtuelles Zielgerät anlegen
                        └── read_loop() + ui.write()  → Event-Forwarding (EV_KEY, EV_ABS, EV_REL)

GNOME Tray
        └── StatusNotifierItem (DBus: org.kde.StatusNotifierItem)
                └── DbusmenuServer (DBus: com.canonical.dbusmenu)
                        └── Event("clicked") → ControllerManager.set_mode()
```

### Virtuelle Zielgeräte

```python
VIRTUAL_XBOX = {
    name:    "Microsoft X-Box 360 pad",
    vendor:  0x045e,  product: 0x028e,
    version: 0x0114,  bustype: BUS_USB,
}
VIRTUAL_PS5 = {
    name:    "Sony Interactive Entertainment DualSense Wireless Controller",
    vendor:  0x054c,  product: 0x0ce6,
    version: 0x8100,  bustype: BUS_BLUETOOTH,
}
```

---

## Wichtige Erkenntnisse & Fallstricke

### Remapper-Thread muss beim Stop aktiv unterbrochen werden
`evdev.read_loop()` blockiert bis ein Event eintrifft. Ein `threading.Event.set()` alleine reicht nicht – der Thread wartet auf den nächsten Controller-Input und ignoriert das Stop-Signal solange der Controller idle ist. **Fix**: beim Stop den File-Deskriptor des Quellgeräts explizit schließen (`dev.close()`), was sofort eine `OSError` im blockierten Thread auslöst.

### UInput-Device wird vor grab() erstellt
Im `Remapper.run()` wird das virtuelle Zielgerät angelegt **bevor** `grab()` versucht wird. Schlägt `grab()` fehl (z.B. weil ein anderer Prozess das Gerät hält), muss `ui.close()` explizit aufgerufen werden – sonst bleibt der virtuelle Device dauerhaft bestehen, obwohl der Remapper nicht läuft.

### Virtuelle Devices im Scanner ausschließen
Der Hotplug-Scanner vergleicht Gerätenamen gegen `VIRTUAL_NAMES`. Ohne diesen Filter würden unsere eigenen virtuellen Geräte als neue physische Controller erkannt und erneut geremappt (Endlosschleife).

### Verwaiste Prozesse nach Migration
Frühere Versionen des Setups (`ds5-to-xbox.py`, `controller-tray.py`) können als verwaiste Hintergrundprozesse weiterlaufen und virtuelle Devices offen halten. Nach einem Service-Neustart prüfen:
```bash
ps aux | grep -E "ds5-to-xbox|controller-tray"
fuser /dev/input/event*   # welche Prozesse halten Input-Devices offen
```

### DBus-Registrierung: Object Path, nicht Bus Name
`org.kde.StatusNotifierWatcher.RegisterStatusNotifierItem()` erwartet den **Object Path** (`/StatusNotifierItem`), nicht den Bus Name. Mit dem Bus Name schlägt die Registrierung stumm fehl und das Icon erscheint nicht.

### bgmod filtert [Env]-Schlüssel
bgmod (GOverlay-Binary, als Lutris `prefix_command`) konstruiert die Prozessumgebung via `execvpe` mit einer expliziten Whitelist. Unbekannte Schlüssel in `bgmod.conf [Env]` werden ignoriert. Umgebungsvariablen können nur über einen Wrapper-Script injiziert werden, der früher im `PATH` liegt als das eigentliche `umu-run`.

> **Differenzierung**: Das von GOverlay pro Spiel erzeugte **`fgmod`** (OptiScaler-Installer, `…/goverlay/gameconfig/<slug>/fgmod`) endet dagegen schlicht auf `"$@"` und **erbt die Umgebung vollständig** – es filtert nichts. Der Env-Filter-Fallstrick betrifft also nur bgmod, nicht dieses fgmod.

### evdev-Grab deckt hidraw nicht ab
Der wichtigste Fallstrick (→ Problem 3): `EVIOCGRAB` sperrt ausschließlich den evdev-Knoten. winebus (Proton) liest Controller über `/dev/hidraw*`. Ein „funktionierender" Grab ist also **keine** Garantie, dass das physische Pad vor dem Spiel versteckt ist – der hidraw-Pfad muss separat geschlossen werden (hidraw-Gate).

---

## Status

- [x] PS5 nativ (kein Remapping)
- [x] PS5 als Xbox ausgeben (Remapping + exklusiver Grab)
- [x] Xbox nativ
- [x] Xbox als PS5 ausgeben
- [x] Tray-Icon mit Menü (StatusNotifierItem + dbusmenu, kein AppIndicator3 nötig)
- [x] Hotplug (Controller ein-/ausstecken, Menü aktualisiert automatisch)
- [x] Persistente Modi-Konfiguration (`~/.config/controller-modes.json`)
- [x] Funktionstest PS5 nativ / PS5 als Xbox in RE: Requiem
- [x] hidraw-Gate gegen Doppeleingabe im Xbox-Modus (Problem 3) – Root-Helper + sudoers + Manager-Integration
- [x] Funktionstest Gothic 1 Remake (UE): keine Doppeleingabe mehr, beide Controllertypen korrekt erkannt
- [x] Gegentest Pragmata (nativ: PS5 via hidraw; xbox: nur virtuelles Pad gelesen, keine Doppeleingabe) – per lsof verifiziert
- [x] Gegentest RE: Requiem (nativ: XInput-only → keine Eingabe, DualSense aber via hidraw sichtbar; xbox: als Xbox erkannt, reagiert) – per lsof verifiziert
- [x] Steam-Spiel Khazan: The First Berserker (Proton-GE) – nativ PS5 / xbox als Xbox, per lsof verifiziert; Steam Input mischt sich bei Flags=0 nicht ein
- [ ] Weitere Steam-Spiele nach Bedarf
- [ ] Multicontroller-Test (mehrere Controller gleichzeitig) – steht noch aus

---

## Vollständiges Script: controller-manager.py

**Pfad**: `~/.local/bin/controller-manager.py`

```python
#!/usr/bin/env python3
"""
Controller Manager — unified tray + per-controller remapping.

Supports:
  PS5 DualSense  →  nativ  |  als Xbox 360 ausgeben
  Xbox Controller →  nativ  |  als PS5 ausgeben
Multiple controllers simultaneously, each with its own mode.
"""

import os, sys, json, signal, threading, time, subprocess
import evdev
from evdev import UInput, ecodes as e
import dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

# ── Konstanten ───────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.expanduser("~/.config/controller-modes.json")

# (vendor, product) → (display name, controller family)
KNOWN_CONTROLLERS = {
    (0x054c, 0x0ce6): ("DualSense",       "ps5"),
    (0x054c, 0x0df2): ("DualSense Edge",  "ps5"),
    (0x045e, 0x028e): ("Xbox 360",        "xbox"),
    (0x045e, 0x028f): ("Xbox 360 W",      "xbox"),
    (0x045e, 0x02ea): ("Xbox One",        "xbox"),
    (0x045e, 0x02fd): ("Xbox One S",      "xbox"),
    (0x045e, 0x0b20): ("Xbox Series X/S", "xbox"),
    (0x045e, 0x0b12): ("Xbox Series X/S", "xbox"),
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
# Ein evdev-Grab sperrt nur den evdev-Knoten; winebus liest /dev/hidraw* direkt.
# Daher chmod 000 auf den hidraw-Knoten des physischen Pads während eines Remaps
# (systemweit, launcher-agnostisch). Braucht Root → eng begrenzter Helper via
# NOPASSWD-sudoers. Fehlt Helper/Regel, ist dies ein stiller No-op.

GATE_BIN = "/usr/local/bin/controller-hidraw-gate"

def hidraw_gate(action, vendor, product):
    """action: 'block' | 'restore'. No-op wenn Helper/sudo-Regel fehlen."""
    if not os.path.exists(GATE_BIN):
        return
    vidpid = f"{vendor:04x}:{product:04x}"
    try:
        subprocess.run(
            ["sudo", "-n", GATE_BIN, action, vidpid],
            timeout=5, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as ex:
        print(f"hidraw_gate: {action} {vidpid} failed: {ex}", file=sys.stderr)


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
    def __init__(self, path, name, vendor, product, family, mode):
        self.path    = path
        self.name    = name
        self.vendor  = vendor
        self.product = product
        self.family  = family
        self.mode    = mode
        self._remap  = None

    def display_name(self):
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
            # Physisches Pad vor Wine/Proton verstecken (Grab deckt hidraw nicht ab)
            hidraw_gate("block", self.vendor, self.product)
            r = Remapper(self.path, target)
            r.start()
            self._remap = r
        else:
            # Nativ: hidraw-Zugriff wieder freigeben
            hidraw_gate("restore", self.vendor, self.product)

    def virtual_path(self):
        return self._remap.virtual_path if self._remap else None

    def stop(self):
        if self._remap:
            self._remap.stop()
            self._remap = None
        # hidraw immer freigeben (Moduswechsel/Abstecken/Shutdown)
        hidraw_gate("restore", self.vendor, self.product)


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
        """Return list of (path, name, vendor, product, family) for real controllers."""
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
            key = (dev.info.vendor, dev.info.product)
            if key in KNOWN_CONTROLLERS:
                dname, family = KNOWN_CONTROLLERS[key]
                if "Motion" not in dev.name and "Touchpad" not in dev.name:
                    result.append((path, dname, dev.info.vendor,
                                   dev.info.product, family))
        return result

    def _monitor(self):
        while True:
            found = {p: (n, v, pr, f) for p, n, v, pr, f in self._scan()}
            changed = False
            with self._lock:
                # Remove disconnected
                for path in list(self._instances):
                    if path not in found:
                        self._instances[path].stop()
                        del self._instances[path]
                        changed = True
                # Add new
                for path, (name, vendor, product, family) in found.items():
                    if path not in self._instances:
                        cfg_key = f"{vendor:04x}:{product:04x}"
                        mode = self._config.get(
                            cfg_key, DEFAULT_MODES.get(family, "ps5-native"))
                        inst = ControllerInstance(
                            path, name, vendor, product, family, mode)
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
            cfg_key = f"{inst.vendor:04x}:{inst.product:04x}"
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
                }))
                id_ += 1

            if inst is not instances[-1]:
                items.append(self._make_item(id_, {"type": dbus.String("separator")}))
                id_ += 1

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
```

---

## Vollständiger Service: controller-manager.service

**Pfad**: `~/.config/systemd/user/controller-manager.service`

```ini
[Unit]
Description=Controller Manager (Tray + Remapping)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=python3 /home/admin/.local/bin/controller-manager.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
```

---

## Vollständiger Helper: controller-hidraw-gate

**Pfad**: `/usr/local/bin/controller-hidraw-gate` (root-eigen, `0755`)

```bash
#!/usr/bin/env bash
# controller-hidraw-gate — block/restore raw HID access to a known game
# controller, so Wine/Proton (winebus) and other userspace cannot read it via
# /dev/hidraw* while controller-manager is remapping it to a virtual pad.
#
#   controller-hidraw-gate block|restore VID:PID
#       VID:PID = 4 hex digits each, e.g. 054c:0ce6
#
# Safety: only VID:PIDs on the built-in allowlist are accepted, and only
# /dev/hidraw* nodes are ever chmod'd. Meant to be invoked through a
# tightly-scoped NOPASSWD sudoers rule. Worst case an authorised caller can
# toggle read access to their own controller's hidraw node — no privesc.

set -euo pipefail

# Known game controllers that have a hidraw node worth gating.
ALLOW=(
  054c:0ce6  # DualSense
  054c:0df2  # DualSense Edge
  054c:05c4  # DualShock 4 (v1)
  054c:09cc  # DualShock 4 (v2)
  045e:0b12  # Xbox Series X/S
  045e:0b13  # Xbox Series X/S (BT)
  045e:02ea  # Xbox One S
  045e:02fd  # Xbox One S (BT)
  045e:0b00  # Xbox Elite 2
)

action="${1:-}"
vidpid="${2:-}"

case "$action" in
  block|restore) ;;
  *) echo "usage: $(basename "$0") block|restore VID:PID" >&2; exit 2 ;;
esac

[[ "$vidpid" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$ ]] || { echo "bad VID:PID '$vidpid'" >&2; exit 2; }
vidpid="${vidpid,,}"

allowed=0
for a in "${ALLOW[@]}"; do [[ "$a" == "$vidpid" ]] && allowed=1; done
[[ $allowed -eq 1 ]] || { echo "VID:PID $vidpid not in allowlist" >&2; exit 3; }

vid="${vidpid%%:*}"
pid="${vidpid##*:}"
# uevent HID_ID looks like: BUS:0000VVVV:0000PPPP (hex, upper-case)
want="0000${vid^^}:0000${pid^^}"

mode=000
[[ "$action" == restore ]] && mode=666

touched=0
for h in /sys/class/hidraw/hidraw*; do
  ue="$h/device/uevent"
  [[ -r "$ue" ]] || continue
  if grep -q "HID_ID=[0-9A-Fa-f]\{4\}:$want\$" "$ue"; then
    node="/dev/$(basename "$h")"
    if chmod "$mode" "$node"; then
      echo "$action $node ($vidpid) -> $mode"
      touched=$((touched + 1))
    fi
  fi
done

echo "controller-hidraw-gate: $action $vidpid touched $touched node(s)"
exit 0
```

### Installation
```bash
sudo install -m 0755 -o root -g root controller-hidraw-gate /usr/local/bin/controller-hidraw-gate
sudo install -m 0440 -o root -g root controller-hidraw.sudoers /etc/sudoers.d/controller-hidraw
sudo visudo -c          # muss "parsed OK" melden
systemctl --user restart controller-manager.service
```

---

## Vollständige sudoers-Datei: controller-hidraw

**Pfad**: `/etc/sudoers.d/controller-hidraw` (`0440`, root-eigen)

```
# Let controller-manager (running as admin) gate raw HID access to remapped
# game controllers without a password. Scoped to the single root-owned helper;
# the helper only ever chmods /dev/hidraw* nodes of allowlisted controllers.
admin ALL=(root) NOPASSWD: /usr/local/bin/controller-hidraw-gate
```

### Verifikation (ohne Spiel)
```bash
# Controller im Tray auf "Als Xbox ausgeben":
ls -l /dev/hidraw8     # erwartet 000  (c---------)
# Tray auf "nativ":
ls -l /dev/hidraw8     # erwartet 666  (crw-rw-rw-)
```
