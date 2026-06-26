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
# Reference doc — kept on the Desktop for convenience (comment out if unwanted).
install -D -m 0644 controller-setup.md         "$HOME/Desktop/controller-setup.md"

echo "==> Root-space (hidraw gate + sudoers) — sudo required"
sudo install -m 0755 -o root -g root controller-hidraw-gate    /usr/local/bin/controller-hidraw-gate
sudo install -m 0440 -o root -g root controller-hidraw.sudoers /etc/sudoers.d/controller-hidraw
sudo visudo -c

echo "==> Reload + restart user service"
systemctl --user daemon-reload
systemctl --user restart controller-manager.service
sleep 1
echo -n "service: "; systemctl --user is-active controller-manager.service

echo "Done."
