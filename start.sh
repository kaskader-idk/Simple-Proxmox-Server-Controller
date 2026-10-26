#!/usr/bin/env bash
# Start this controller with one command after cloning: sudo bash start.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root on the Proxmox host: sudo bash start.sh" >&2
  exit 1
fi
dev="$(printenv PVE_CONTROLLER_DEV || echo 0)"
if ! command -v pvesh >/dev/null && [[ "$dev" != "1" ]]; then
  echo "This script must run on a Proxmox VE node (pvesh missing)." >&2
  exit 1
fi
if ! command -v python3 >/dev/null; then
  echo "Python 3 is required." >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  if ! python3 -m venv .venv; then
    echo "Installing python3-venv and python3-pip..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3-venv python3-pip
    python3 -m venv .venv
  fi
fi
want="$(sha256sum requirements.txt | cut -d' ' -f1)"
have="$(cat .venv/.requirements.sha 2>/dev/null || true)"
if [[ "$want" != "$have" ]]; then
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
  printf '%s\n' "$want" > .venv/.requirements.sha
fi
bind="$(printenv PVE_CONTROLLER_BIND || echo 0.0.0.0)"
port="$(printenv PVE_CONTROLLER_PORT || echo 8787)"
echo
echo "Simple Proxmox Controller — http://<PROXMOX-LAN-IP>:$port"
echo "WARNING: No login. Full guest administration. NEVER expose this port publicly."
echo "Only direct LAN and Tailscale peers are allowed."
echo
exec .venv/bin/uvicorn controller.main:app --host "$bind" --port "$port" --no-proxy-headers
