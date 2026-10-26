# Simple Proxmox Server Controller

A responsive, **no-login** web dashboard for monitoring and managing a **local Proxmox VE node**. It shows CPU threads, RAM, swap, disk and network resources; CT/VM status and allocations; guest details; a Linux guest file explorer; and a browser terminal.

> **SECURITY WARNING — READ BEFORE STARTING**
>
> The application runs with **root privileges on the Proxmox host**, has **no login** by design, and provides guest filesystem and shell access. Anyone on an allowed network who can reach port 8787 can administer your guests. **Never port-forward it or put it on a public URL / public Cloudflare Tunnel.** The app rejects direct connections outside RFC1918, loopback, and Tailscale (100.64.0.0/10) networks and checks browser Origin on writes/WebSockets. A reverse proxy may bypass this source-address guard; do not publish it via one. For access outside home, connect through a private VPN/Tailscale with appropriate ACLs.

## Quick start

On a **Proxmox host**, not inside a normal VM/LXC:

```bash
git clone https://github.com/kaskader-idk/Simple-Proxmox-Server-Controller.git
cd Simple-Proxmox-Server-Controller
sudo bash start.sh
```

Open **http://PROXMOX-LAN-IP:8787** in a browser. The start script creates a Python virtual environment, installs dependencies on first use and starts the server. It needs Python 3.10+, Proxmox tools, and an internet connection on first start. Keep the terminal open while using it.

For automatic startup instead: `sudo bash install-service.sh` (then `systemctl status simple-proxmox-controller`). To update: `git pull && sudo systemctl restart simple-proxmox-controller`. Adjust the bind address and port with `PVE_CONTROLLER_BIND` / `PVE_CONTROLLER_PORT` environment variables; bind to `127.0.0.1` for local-only access.

## Dashboard

- Host CPU name/topology and **per-logical-thread usage**; CPU clock and load.
- Real-time RAM and swap, mounted storage usage, network interfaces, available temperature sensors, GPU descriptions, uptime, kernel and top memory-consuming processes.
- LXC and QEMU guest cards with running/stopped state, assigned CPUs, memory and swap usage/reservations, disk allocation/usage and cumulative I/O (as reported by Proxmox).
- Detailed view: Proxmox guest configuration, reserved CPU threads, memory, swap, virtual disks and guest OS-specific readings including **per-thread CPU samples**, when readable.

**Storage clarification:** for VMs, the host often knows disk *provisioned size* but not filesystem *used space*. Accurate in-guest used space requires a running Linux guest with QEMU Guest Agent and Python 3. The guest's root filesystem is shown separately from its virtual disk allocation. VM swap is also managed inside the guest, rather than reserved by Proxmox.

## Guest file explorer

Use the Files tab to browse any accessible absolute Linux guest directory, open folders, download files, upload via picker or drag-and-drop, rename, move (including dropping on folders), copy, create directories, delete, create ZIP or tar.gz archives, and extract ZIP/tar/tar.gz archives. A Chrome-family browser may support dragging files from the page to the desktop through DownloadURL; other browsers may require Download.

Requirements:

| Guest | Requirements |
| --- | --- |
| Running Linux LXC | Python 3 installed in container, and working `pct exec` |
| Running Linux VM | QEMU Guest Agent enabled in Proxmox and running in VM; Python 3 installed in VM |
| Windows / other OS | Resource/status monitoring works; guest file explorer and one-off shell commands are **not supported** by this Linux helper |

Install guest dependencies as needed, e.g. inside Debian/Ubuntu Linux VMs: `sudo apt install qemu-guest-agent python3` and `sudo systemctl enable --now qemu-guest-agent`, then enable **Options → QEMU Guest Agent** in Proxmox. For Debian/Ubuntu LXC: `pct exec VMID -- apt install -y python3`.

Uploads are sent in 32 KiB chunks through the guest execution channel; downloads are streamed in 64 KiB chunks. Large transfers through the QEMU Agent can be slower than SSH/SFTP. Uploads use a temporary file in the destination directory and refuse to overwrite an existing file. Archive extraction blocks parent path traversal, links and special files, and caps uncompressed archive size at 2 GiB.

## Terminal and power controls

- LXC terminal is a PTY attached via `pct enter VMID`.
- QEMU terminal uses `qm terminal VMID`. The VM needs **a serial device** (`serial0`) and a guest serial login console. If not configured, use the **Execute command** panel instead, which runs independent Linux commands via QEMU Guest Agent.
- Start, graceful shutdown, reboot and force stop buttons are provided. Force stop can lose data.

**Important:** the browser terminal is an elementary text PTY viewer, not a full xterm emulator; complex TUI applications and terminal colors may display imperfectly.

## Internals

- FastAPI/uvicorn and psutil, no external database or JavaScript build tooling.
- Proxmox `pvesh`/`pct`/`qm` run on the **same node** where the app starts. This is a **single-node controller**; cluster-wide remote guest operations are not yet supported.
- A short Python helper is executed inside a Linux guest on request; **no resident agent installed by this project**.
- Polling interval: 10 seconds. Live guest filesystem/CPU samples are fetched on demand.
- HTTP and WS are guarded by peer network and browser Origin. No session cookies or authentication.
- Errors from stopped guests, missing QEMU Guest Agent or guest Python are shown in the UI.

Run unit tests after installation:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

### Troubleshooting

- **Page does not load:** verify the service with `systemctl status simple-proxmox-controller`, use the Proxmox host IP and port 8787, and check the firewall for LAN access.
- **503 / missing pvesh:** run on the actual Proxmox host as root.
- **VM file browser errors:** enable QEMU Guest Agent, start its service inside the VM, and install Python 3.
- **VM console shows serial warning:** configure a Proxmox serial port and enable serial console/login inside the VM, or use one-off Command mode.
- **Remote browser blocked:** do not publicly expose the web server. Use Tailscale or another private VPN.
