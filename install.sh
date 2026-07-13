#!/usr/bin/env bash
# Deploy controller-manager from this repo into the live system locations.
#
# User-space files need no privileges; the hidraw gate (root-owned helper) and
# its sudoers rule require sudo. Run from anywhere:  ./install.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

echo "==> User-space (controller-manager + service)"
install -D -m 0755 controller-manager.py       "$HOME/.local/bin/controller-manager.py"
install -D -m 0644 controller-manager.service  "$HOME/.config/systemd/user/controller-manager.service"

echo "==> Root-space (hidraw gate + led helper + udev rule + sudoers) — sudo required"
sudo install -m 0755 -o root -g root controller-hidraw-gate /usr/local/bin/controller-hidraw-gate
sudo install -m 0755 -o root -g root controller-led         /usr/local/bin/controller-led

# Gate udev rule: gated hidraw nodes must be born inaccessible (MODE 0000,
# no uaccess ACL), otherwise Steam re-opens them the instant the gate's
# driver rebind recreates them. The 72- prefix must outrank the 71-*
# controller rules from steam-devices (they re-add uaccess for Sony pads).
# Remove the superseded 71- name from earlier installs, then reload rules
# so everything applies without a reboot.
sudo install -m 0644 -o root -g root 72-controller-manager.rules /etc/udev/rules.d/72-controller-manager.rules
sudo rm -f /etc/udev/rules.d/71-controller-manager.rules
sudo udevadm control --reload
# Replay hidraw events so the new rule is evaluated for already-connected pads
# too (no markers exist yet, so nodes simply stay open — this only makes the
# rule effective now instead of at the next reconnect).
sudo udevadm trigger --subsystem-match=hidraw

# Render the sudoers rule for the installing user, validate it in isolation,
# then install it. Validating before placement avoids leaving a broken file in
# /etc/sudoers.d.
rendered_sudoers="$(mktemp)"
trap 'rm -f "$rendered_sudoers"' EXIT
sed "s/__INSTALL_USER__/$(id -un)/" controller-hidraw.sudoers > "$rendered_sudoers"
sudo visudo -cf "$rendered_sudoers"
sudo install -m 0440 -o root -g root "$rendered_sudoers" /etc/sudoers.d/controller-hidraw
sudo visudo -c

echo "==> Reload + enable (autostart) + restart user service"
systemctl --user daemon-reload
# enable (without --now) links the service into the session's autostart so it
# comes up on the next login; restart applies the freshly installed code to the
# already-running instance (which enable --now would leave untouched).
systemctl --user enable controller-manager.service
systemctl --user restart controller-manager.service
sleep 1
# Report the state without aborting on a non-active service (is-active exits
# non-zero then, which `set -e` would turn into a silent abort BEFORE the
# hint below). A failed start is usually a missing Python runtime dep.
if systemctl --user is-active --quiet controller-manager.service; then
    echo "service: active"
    echo "Done."
else
    echo "service: NOT active" >&2
    echo "check:  journalctl --user -u controller-manager.service -n 30" >&2
    exit 1
fi
