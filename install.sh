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

echo "==> Root-space (hidraw gate + led helper + sudoers) — sudo required"
sudo install -m 0755 -o root -g root controller-hidraw-gate /usr/local/bin/controller-hidraw-gate
sudo install -m 0755 -o root -g root controller-led         /usr/local/bin/controller-led

# Render the sudoers rule for the installing user, validate it in isolation,
# then install it. Validating before placement avoids leaving a broken file in
# /etc/sudoers.d.
rendered_sudoers="$(mktemp)"
trap 'rm -f "$rendered_sudoers"' EXIT
sed "s/__INSTALL_USER__/$(id -un)/" controller-hidraw.sudoers > "$rendered_sudoers"
sudo visudo -cf "$rendered_sudoers"
sudo install -m 0440 -o root -g root "$rendered_sudoers" /etc/sudoers.d/controller-hidraw
sudo visudo -c

echo "==> Reload + restart user service"
systemctl --user daemon-reload
systemctl --user restart controller-manager.service
sleep 1
echo -n "service: "; systemctl --user is-active controller-manager.service

echo "Done."
