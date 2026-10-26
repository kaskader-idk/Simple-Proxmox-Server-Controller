#!/usr/bin/env bash
# Optional: install as persistent systemd service.
set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run: sudo bash install-service.sh" >&2
  exit 1
fi
cd "$(dirname "$(readlink -f "$0")")"
root="$(pwd)"
if [[ "$root" == *" "* ]]; then
  echo "Move this repository to a path without spaces." >&2
  exit 1
fi
cat > /etc/systemd/system/simple-proxmox-controller.service <<EOF
[Unit]
Description=Simple Proxmox Server Controller
After=network-online.target pve-cluster.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$root
ExecStart=/bin/bash $root/start.sh
Restart=on-failure
RestartSec=5
Environment=PVE_CONTROLLER_BIND=0.0.0.0
Environment=PVE_CONTROLLER_PORT=8788

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable simple-proxmox-controller.service
systemctl restart simple-proxmox-controller.service
systemctl --no-pager status simple-proxmox-controller.service
