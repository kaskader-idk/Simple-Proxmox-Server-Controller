"""Proxmox node / guest inventory and hardware monitoring."""
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time

import psutil
from .guest import GuestError, guest_call

NODE = socket.gethostname().split(".")[0]

def run(*args, timeout=15):
    try:
        completed = subprocess.run(list(args), capture_output=True, text=True,
                                   errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "Command failed")[-1500:])
    return completed.stdout

def pvesh(path):
    return json.loads(run("pvesh", "get", path, "--output-format", "json"))

def bytesize_from_config(value):
    """Parse Proxmox volume size option such as size=8G or size=512M."""
    matched = re.search(r"(?:^|,)size=(\d+(?:\.\d+)?)([KMGT]?)", str(value), re.I)
    if not matched:
        return 0
    return int(float(matched.group(1)) * 1024 ** ("KMGT".index(matched.group(2).upper()) + 1 if matched.group(2) else 0))

def guest_id(kind, vmid):
    if kind not in ("lxc", "qemu") or not 100 <= int(vmid) <= 999999999:
        raise ValueError("Unknown guest")
    return int(vmid)

def guests():
    result = []
    for kind in ("lxc", "qemu"):
        entries = pvesh("/nodes/" + NODE + "/" + kind)
        for item in entries:
            vmid = int(item["vmid"])
            status = item.get("status", "unknown")
            result.append({
                "kind": kind, "vmid": vmid,
                "name": item.get("name") or item.get("hostname") or str(vmid),
                "status": status, "cpu": item.get("cpu", 0),
                "used_mem": item.get("mem", 0), "max_mem": item.get("maxmem", 0),
                "used_swap": item.get("swap", 0), "max_swap": item.get("maxswap", 0),
                "used_disk": item.get("disk", 0), "max_disk": item.get("maxdisk", 0),
                "uptime": item.get("uptime", 0), "cpus": item.get("cpus", 0),
                "netin": item.get("netin", 0), "netout": item.get("netout", 0),
                "diskread": item.get("diskread", 0), "diskwrite": item.get("diskwrite", 0)
            })
    return sorted(result, key=lambda x: (x["kind"] != "lxc", x["vmid"]))

def guest_detail(kind, vmid):
    vmid = guest_id(kind, vmid)
    base = "/nodes/" + NODE + "/" + kind + "/" + str(vmid)
    config = pvesh(base + "/config")
    status = pvesh(base + "/status/current")
    if kind == "lxc":
        cores = int(config.get("cores") or config.get("cpulimit") or (os.cpu_count() or 1))
        swap_reserved = int(config.get("swap") or 0) * 1024 ** 2
        disks = [{"device": key, "configuration": value,
                  "reserved_bytes": bytesize_from_config(value)}
                 for key, value in config.items()
                 if key == "rootfs" or re.fullmatch(r"mp\d+", key)]
    else:
        cores = int(config.get("cores", 1)) * int(config.get("sockets", 1))
        swap_reserved = 0  # QEMU doesn't reserve guest swap; requires in-guest reading.
        disks = [{"device": key, "configuration": value,
                  "reserved_bytes": bytesize_from_config(value)}
                 for key, value in config.items()
                 if re.fullmatch(r"(scsi|virtio|sata|ide)\d+", key)]
    output = {
        "kind": kind, "vmid": vmid, "node": NODE,
        "name": config.get("hostname") or config.get("name") or str(vmid),
        "status": status.get("status"), "config": config, "raw_status": status,
        "reserved": {
            "memory": int(config.get("memory") or 0) * 1024 ** 2,
            "swap": swap_reserved, "cpu_threads": cores,
            "disks": disks
        }, "guest": None, "guest_error": None
    }
    if status.get("status") == "running":
        try:
            output["guest"] = guest_call(kind, vmid, "info")
        except (GuestError, ValueError) as exc:
            output["guest_error"] = str(exc)
    return output

def _cpu_topology():
    result = []
    for index in range(psutil.cpu_count(logical=True) or 0):
        prefix = "/sys/devices/system/cpu/cpu" + str(index) + "/topology/"
        try:
            with open(prefix + "core_id") as f:
                core = f.read().strip()
            with open(prefix + "physical_package_id") as f:
                package = f.read().strip()
        except OSError:
            core, package = str(index), "0"
        result.append({"thread": index, "core": core, "package": package})
    return result

def _gpus():
    result = []
    if shutil.which("lspci"):
        try:
            for line in run("lspci", timeout=3).splitlines():
                if any(key in line.lower() for key in ("vga compatible", "3d controller", "display controller")):
                    result.append(line)
        except RuntimeError:
            pass
    return result


def _read_text(file):
    try:
        with open(file, encoding="utf-8", errors="replace") as handle:
            return handle.read(4096).strip()
    except OSError:
        return ""

def _firmware():
    keys = {
        "manufacturer": "sys_vendor",
        "system": "product_name",
        "board_vendor": "board_vendor",
        "motherboard": "board_name",
        "board_version": "board_version",
        "bios_vendor": "bios_vendor",
        "bios_version": "bios_version",
        "bios_date": "bios_date",
    }
    return {name: _read_text("/sys/class/dmi/id/" + file)
            for name, file in keys.items()}

def _software():
    os_name = ""
    for line in _read_text("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip('"')
    info = {"distribution": os_name, "proxmox_version": "",
            "components": [], "running_services": []}
    if shutil.which("pveversion"):
        try:
            lines = run("pveversion", "--verbose", timeout=6).splitlines()
            info["proxmox_version"] = lines[0] if lines else ""
            info["components"] = lines[:35]
        except RuntimeError:
            pass
    if shutil.which("systemctl"):
        try:
            lines = run("systemctl", "list-units", "--type=service",
                        "--state=running", "--no-legend", "--no-pager",
                        timeout=6).splitlines()
            info["running_services"] = [line.split()[0] for line in lines
                                        if line.split() and line.split()[0].endswith(".service")][:80]
        except RuntimeError:
            pass
    return info

def _usb():
    if not shutil.which("lsusb"):
        return []
    try:
        return run("lsusb", timeout=3).splitlines()[:100]
    except RuntimeError:
        return []

def node_status():
    per_thread = psutil.cpu_percent(interval=0.15, percpu=True)
    virtual_mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({"device": part.device, "mount": part.mountpoint,
                          "filesystem": part.fstype, "total": usage.total,
                          "used": usage.used, "free": usage.free, "percent": usage.percent})
        except (PermissionError, OSError):
            pass
    sensors = {}
    try:
        for label, readings in psutil.sensors_temperatures().items():
            sensors[label] = [{"label": x.label or label, "current": x.current,
                               "high": x.high, "critical": x.critical}
                              for x in readings]
    except (AttributeError, OSError):
        pass
    network = []
    counters = psutil.net_io_counters(pernic=True)
    for name, stats in psutil.net_if_stats().items():
        if name in counters:
            network.append({"name": name, "up": stats.isup, "speed": stats.speed,
                            "sent": counters[name].bytes_sent,
                            "received": counters[name].bytes_recv})
    cpu_freq = psutil.cpu_freq()
    top_processes = []
    for p in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]):
        try:
            top_processes.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    top_processes.sort(key=lambda x: x.get("memory_percent") or 0, reverse=True)
    return {
        "node": NODE, "hostname": socket.gethostname(), "platform": platform.platform(),
        "kernel": platform.release(), "python": platform.python_version(),
        "cpu_name": _cpu_name(), "cpu_threads": per_thread,
        "cpu_topology": _cpu_topology(), "cpu_total": psutil.cpu_percent(interval=None),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "cpu_freq": cpu_freq._asdict() if cpu_freq else None,
        "memory": {"total": virtual_mem.total, "used": virtual_mem.used,
                   "available": virtual_mem.available, "percent": virtual_mem.percent},
        "swap": {"total": swap.total, "used": swap.used, "free": swap.free},
        "disks": disks, "temperatures": sensors, "networks": network,
        "gpus": _gpus(), "usb": _usb(), "firmware": _firmware(),
        "software": _software(), "uptime": int(time.time() - psutil.boot_time()),
        "load_average": os.getloadavg() if hasattr(os, "getloadavg") else [],
        "processes": top_processes[:12],
        "proxmox": shutil.which("pvesh") is not None
    }

def _cpu_name():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()

def lifecycle(kind, vmid, action):
    guest_id(kind, vmid)
    if action not in ("start", "shutdown", "reboot", "stop"):
        raise ValueError("Invalid action")
    return run("pct" if kind == "lxc" else "qm", action, str(vmid), timeout=20).strip()
