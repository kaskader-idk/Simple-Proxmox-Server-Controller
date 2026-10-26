"""Operations executed inside a Linux CT or QEMU-agent-enabled Linux VM.

No HTTP service is installed in guests. The host invokes this small Python
helper through pct exec or qm guest exec. All filesystem paths refer to the guest.
"""
import base64
import json
import re
import subprocess

MARKER = "@@SPC@@"
GUEST_SCRIPT = r"""
import base64, json, os, platform, shutil, stat, sys, tarfile, time, zipfile

def result(value):
    payload = base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False).encode()).decode()
    print("@@SPC@@" + payload, flush=True)

def path(value):
    if not isinstance(value, str) or not value.startswith("/") or chr(0) in value or len(value) > 4096:
        raise ValueError("A valid absolute guest path is required")
    return os.path.normpath(value)

def size_info(p):
    st = os.lstat(p)
    return {
        "name": os.path.basename(p.rstrip("/")) or "/",
        "path": p, "directory": stat.S_ISDIR(st.st_mode),
        "link": stat.S_ISLNK(st.st_mode), "size": st.st_size,
        "modified": int(st.st_mtime), "mode": stat.filemode(st.st_mode)
    }

def listing(p):
    if not os.path.isdir(p):
        raise NotADirectoryError(p)
    items = []
    with os.scandir(p) as entries:
        for index, item in enumerate(entries):
            if index >= 2000:
                break
            try:
                items.append(size_info(item.path))
            except (PermissionError, FileNotFoundError):
                pass
    items.sort(key=lambda x: (not x["directory"], x["name"].casefold()))
    disk = os.statvfs(p)
    return {"path": p, "items": items, "limited": len(items) == 2000,
            "filesystem": {"size": disk.f_blocks * disk.f_frsize,
                           "free": disk.f_bavail * disk.f_frsize}}

def cpu_snapshot():
    cores = []
    with open("/proc/stat") as f:
        for line in f:
            fields = line.split()
            if not fields or not fields[0].startswith("cpu"):
                break
            values = [int(x) for x in fields[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            cores.append((sum(values), idle))
    return cores

def info():
    before = cpu_snapshot()
    time.sleep(0.15)
    after = cpu_snapshot()
    cpus = []
    for a, b in zip(before[1:], after[1:]):
        total = b[0] - a[0]
        cpus.append(round(100 * (1 - (b[1] - a[1]) / total), 1) if total > 0 else 0)
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            name, value = line.split(":", 1)
            mem[name] = int(value.strip().split()[0]) * 1024
    root = os.statvfs("/")
    return {
        "hostname": platform.node(), "os": platform.platform(),
        "kernel": platform.release(), "architecture": platform.machine(),
        "cpu_threads": cpus,
        "memory_total": mem.get("MemTotal", 0),
        "memory_used": mem.get("MemTotal", 0) - mem.get("MemAvailable", mem.get("MemFree", 0)),
        "swap_total": mem.get("SwapTotal", 0),
        "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
        "disk_total": root.f_blocks * root.f_frsize,
        "disk_used": (root.f_blocks - root.f_bfree) * root.f_frsize
    }

def safe_archive_target(root, name):
    if name.startswith("/") or "\\" in name:
        raise ValueError("Unsafe archive path")
    target = os.path.abspath(os.path.join(root, name))
    if (os.path.commonpath([root, target]) != root or
        os.path.commonpath([root, os.path.realpath(target)]) != root):
        raise ValueError("Unsafe archive path or symlink traversal")
    return target

def extract(archive, target):
    os.makedirs(target, exist_ok=True)
    target = os.path.realpath(target)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            total = 0
            for entry in z.infolist():
                safe_archive_target(target, entry.filename)
                mode = (entry.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise ValueError("Archive symlinks are not allowed")
                total += entry.file_size
                if total > 2 * 1024 ** 3 or len(z.infolist()) > 10000:
                    raise ValueError("Archive is too large")
            for entry in z.infolist():
                dst = safe_archive_target(target, entry.filename)
                if entry.is_dir():
                    os.makedirs(dst, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.islink(os.path.dirname(dst)):
                        raise ValueError("Unsafe archive destination")
                    with z.open(entry) as source, open(dst, "wb") as sink:
                        shutil.copyfileobj(source, sink)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            members = t.getmembers()
            if len(members) > 10000 or sum(x.size for x in members) > 2 * 1024 ** 3:
                raise ValueError("Archive is too large")
            for entry in members:
                safe_archive_target(target, entry.name)
                if not (entry.isdir() or entry.isfile()):
                    raise ValueError("Archive links and special files are not allowed")
            for entry in members:
                dst = safe_archive_target(target, entry.name)
                if entry.isdir():
                    os.makedirs(dst, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with t.extractfile(entry) as source, open(dst, "wb") as sink:
                        shutil.copyfileobj(source, sink)
    else:
        raise ValueError("Only .zip, .tar, .tar.gz and .tgz archives can be extracted")
    return {"ok": True}

def execute():
    op, *args = sys.argv[1:]
    if op == "list":
        return listing(path(args[0]))
    if op == "info":
        return info()
    if op == "read":
        p, offset, limit = path(args[0]), int(args[1]), min(65536, int(args[2]))
        with open(p, "rb") as f:
            f.seek(offset)
            data = f.read(limit)
            more = f.read(1)
        return {"chunk": base64.b64encode(data).decode(), "eof": not bool(more)}
    if op == "write":
        p, mode = path(args[0]), args[1]
        if mode not in ("wb", "ab"):
            raise ValueError("Invalid write mode")
        payload = base64.b64decode(args[2], validate=True)
        with open(p, mode) as f:
            f.write(payload)
        return {"ok": True}
    if op == "mkdir":
        os.mkdir(path(args[0]))
        return {"ok": True}
    if op == "move":
        source, target = path(args[0]), path(args[1])
        if os.path.lexists(target):
            raise FileExistsError(target)
        shutil.move(source, target)
        return {"ok": True}
    if op == "copy":
        source, target = path(args[0]), path(args[1])
        if os.path.lexists(target):
            raise FileExistsError(target)
        if os.path.isdir(source) and not os.path.islink(source):
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        return {"ok": True}
    if op == "replace":
        source, target = path(args[0]), path(args[1])
        os.replace(source, target)
        return {"ok": True}
    if op == "delete":
        p = path(args[0])
        if p == "/":
            raise ValueError("Cannot delete filesystem root")
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
        return {"ok": True}
    if op == "compress":
        source, archive, fmt = path(args[0]), path(args[1]), args[2]
        if os.path.lexists(archive):
            raise FileExistsError(archive)
        if fmt == "zip":
            with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                if os.path.isdir(source):
                    for root, dirs, files in os.walk(source):
                        for name in files:
                            p = os.path.join(root, name)
                            z.write(p, os.path.relpath(p, os.path.dirname(source)))
                else:
                    z.write(source, os.path.basename(source))
        elif fmt == "tar.gz":
            with tarfile.open(archive, "x:gz") as t:
                t.add(source, arcname=os.path.basename(source))
        else:
            raise ValueError("Unknown archive format")
        return {"ok": True}
    if op == "extract":
        return extract(path(args[0]), path(args[1]))
    if op == "command":
        import subprocess
        command = args[0]
        proc = subprocess.run(["/bin/sh", "-lc", command], capture_output=True,
                              text=True, errors="replace", timeout=30)
        return {"output": (proc.stdout + proc.stderr)[-30000:], "exitcode": proc.returncode}
    raise ValueError("Unknown operation")

try:
    result(execute())
except Exception as e:
    result({"error": type(e).__name__ + ": " + str(e)})
"""

class GuestError(RuntimeError):
    pass

def _run(argv, timeout=90):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuestError(str(exc)) from exc
    if proc.returncode != 0:
        raise GuestError((proc.stderr or proc.stdout or "Guest command failed")[-1800:])
    return proc.stdout

def _decode(text):
    matches = re.findall(r"@@SPC@@([A-Za-z0-9_=-]+)", text)
    if not matches:
        # Some QEMU agent frontends expose the entire stdout as base64.
        try:
            import yaml
            document = yaml.safe_load(text)
            if isinstance(document, dict):
                output = document.get("out-data", "")
                if isinstance(output, str):
                    matches = re.findall(r"@@SPC@@([A-Za-z0-9_=-]+)", output)
                    if not matches:
                        candidate = base64.b64decode(output, validate=True).decode("utf-8")
                        matches = re.findall(r"@@SPC@@([A-Za-z0-9_=-]+)", candidate)
        except (ValueError, UnicodeError, ImportError):
            pass
    if not matches:
        raise GuestError("Guest did not return a valid response. For VMs enable the QEMU guest agent; for Linux guests install python3.")
    try:
        data = json.loads(base64.urlsafe_b64decode(matches[-1]).decode())
    except (ValueError, UnicodeError) as exc:
        raise GuestError("Unable to parse guest response") from exc
    if isinstance(data, dict) and "error" in data:
        raise GuestError(data["error"])
    return data

def guest_call(kind, vmid, operation, *args):
    if kind not in ("lxc", "qemu") or not (100 <= int(vmid) <= 999999999):
        raise GuestError("Invalid guest type or ID")
    command = ["/usr/bin/env", "python3", "-c", GUEST_SCRIPT, operation,
               *(str(x) for x in args)]
    if kind == "lxc":
        argv = ["pct", "exec", str(vmid), "--", *command]
    else:
        argv = ["qm", "guest", "exec", str(vmid), "--timeout", "60", "--", *command]
    return _decode(_run(argv))

def guest_command(kind, vmid, command):
    if len(command) > 8192:
        raise GuestError("Command is too long")
    return guest_call(kind, vmid, "command", command)
