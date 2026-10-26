"""LAN-only, passwordless Proxmox web controller.

Run as root on a trusted Proxmox node. Do NOT expose this service to the
public internet: it provides full administrative guest shell/file access.
"""
import asyncio
import base64
import ipaddress
import json
import os
import pty
import re
import secrets
import signal
import struct
import fcntl
import termios
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from . import proxmox
from .guest import GuestError, guest_call, guest_command

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="Simple Proxmox Server Controller", docs_url=None, redoc_url=None,
              openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

PRIVATE_RANGES = tuple(ipaddress.ip_network(x) for x in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"
))

def allowed_peer(host):
    try:
        addr = ipaddress.ip_address(host)
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in subnet for subnet in PRIVATE_RANGES)
    except ValueError:
        return False

def origin_ok(origin, host):
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc.lower() == host.lower()
    except ValueError:
        return False

@app.middleware("http")
async def security(request: Request, call_next):
    if not request.client or not allowed_peer(request.client.host):
        return JSONResponse({"error": "LAN/Tailscale access only"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not origin_ok(request.headers.get("origin"), request.headers.get("host", "")):
            return JSONResponse({"error": "Cross-origin write refused"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "connect-src 'self' ws: wss:; img-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response

@app.exception_handler(GuestError)
async def guest_error_handler(request: Request, exc: GuestError):
    return JSONResponse(status_code=422, content={"error": str(exc)})

@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    return JSONResponse(status_code=503, content={"error": str(exc)})

def check_guest(kind, vmid):
    try:
        return proxmox.guest_id(kind, vmid)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid guest")

@app.get("/")
def home():
    return FileResponse(ROOT / "web" / "index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "node": proxmox.NODE,
            "proxmox": proxmox.shutil.which("pvesh") is not None}

@app.get("/api/node")
async def node():
    return await asyncio.to_thread(proxmox.node_status)

@app.get("/api/guests")
async def guests():
    return await asyncio.to_thread(proxmox.guests)

@app.get("/api/guest/{kind}/{vmid}")
async def guest_detail(kind: str, vmid: int):
    check_guest(kind, vmid)
    return await asyncio.to_thread(proxmox.guest_detail, kind, vmid)

@app.get("/api/guest/{kind}/{vmid}/files")
async def browse(kind: str, vmid: int, path: str = "/"):
    check_guest(kind, vmid)
    return await asyncio.to_thread(guest_call, kind, vmid, "list", path)

class Action(BaseModel):
    op: str
    path: str
    destination: str = ""
    format: str = "zip"

@app.post("/api/guest/{kind}/{vmid}/files/action")
async def file_action(kind: str, vmid: int, body: Action):
    check_guest(kind, vmid)
    op = body.op
    if op not in ("mkdir", "move", "copy", "replace", "delete", "compress", "extract"):
        raise HTTPException(status_code=400, detail="Unsupported file operation")
    if op in ("move", "copy", "replace", "compress", "extract") and not body.destination:
        raise HTTPException(status_code=400, detail="Destination required")
    args = [body.path]
    if op in ("move", "copy", "replace", "compress", "extract"):
        args.append(body.destination)
    if op == "compress":
        if body.format not in ("zip", "tar.gz"):
            raise HTTPException(status_code=400, detail="Unknown archive format")
        args.append(body.format)
    return await asyncio.to_thread(guest_call, kind, vmid, op, *args)

@app.post("/api/guest/{kind}/{vmid}/files/upload")
async def upload(kind: str, vmid: int, request: Request, file: UploadFile = File(...),
                 path: str = "/"):
    check_guest(kind, vmid)
    if not path.startswith("/") or "\0" in path:
        raise HTTPException(status_code=400, detail="Invalid directory")
    name = (file.filename or "").replace("\\", "/").split("/")[-1]
    if not name or name in (".", "..") or "\0" in name or len(name) > 255:
        raise HTTPException(status_code=400, detail="Invalid filename")
    parent = path.rstrip("/") or "/"
    dest = parent.rstrip("/") + "/" + name
    temp = parent.rstrip("/") + "/.spc-upload-" + secrets.token_hex(12)
    size = 0
    try:
        first = True
        while True:
            chunk = await file.read(32768)
            if not chunk:
                break
            size += len(chunk)
            await asyncio.to_thread(guest_call, kind, vmid, "write",
                                    temp, "wb" if first else "ab",
                                    base64.b64encode(chunk).decode())
            first = False
        if first:
            await asyncio.to_thread(guest_call, kind, vmid, "write", temp, "wb", "")
        # Prevent accidental clobbering. Existing destinations fail explicitly.
        entries = await asyncio.to_thread(guest_call, kind, vmid, "list", parent)
        if any(x["name"] == name for x in entries["items"]):
            raise GuestError("File already exists: " + name)
        await asyncio.to_thread(guest_call, kind, vmid, "move", temp, dest)
        return {"ok": True, "name": name, "size": size}
    except Exception:
        try:
            await asyncio.to_thread(guest_call, kind, vmid, "delete", temp)
        except Exception:
            pass
        raise
    finally:
        await file.close()

@app.get("/api/guest/{kind}/{vmid}/files/download")
async def download(kind: str, vmid: int, path: str):
    check_guest(kind, vmid)
    if not path.startswith("/") or path == "/" or "\0" in path:
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Read the first chunk up front, so failures become 4xx rather than truncated 200s.
    first = await asyncio.to_thread(guest_call, kind, vmid, "read", path, 0, 65536)
    async def chunks():
        response = first
        offset = 0
        while True:
            data = base64.b64decode(response["chunk"])
            yield data
            offset += len(data)
            if response["eof"] or not data:
                break
            response = await asyncio.to_thread(guest_call, kind, vmid, "read", path, offset, 65536)
    name = path.rsplit("/", 1)[-1]
    return StreamingResponse(chunks(), media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")})

class Command(BaseModel):
    command: str = Field(min_length=1, max_length=8192)

@app.post("/api/guest/{kind}/{vmid}/command")
async def command(kind: str, vmid: int, body: Command):
    check_guest(kind, vmid)
    return await asyncio.to_thread(guest_command, kind, vmid, body.command)

@app.post("/api/guest/{kind}/{vmid}/power/{action}")
async def power(kind: str, vmid: int, action: str):
    check_guest(kind, vmid)
    if action not in ("start", "shutdown", "reboot", "stop"):
        raise HTTPException(status_code=400, detail="Invalid power action")
    output = await asyncio.to_thread(proxmox.lifecycle, kind, vmid, action)
    return {"ok": True, "output": output}

def resize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

@app.websocket("/ws/{kind}/{vmid}/terminal")
async def terminal(ws: WebSocket, kind: str, vmid: int):
    if not ws.client or not allowed_peer(ws.client.host) or not origin_ok(
        ws.headers.get("origin"), ws.headers.get("host", "")
    ):
        await ws.close(code=1008)
        return
    try:
        check_guest(kind, vmid)
        if kind == "qemu":
            cfg = await asyncio.to_thread(proxmox.pvesh, "/nodes/" + proxmox.NODE
                                          + "/qemu/" + str(vmid) + "/config")
            if not any(x.startswith("serial") for x in cfg):
                await ws.accept()
                await ws.send_text("VM serial console is not configured. Use the Command tab and QEMU Guest Agent, or configure serial0.\r\n")
                await ws.close()
                return
        command = ["pct", "enter", str(vmid)] if kind == "lxc" else ["qm", "terminal", str(vmid)]
        master, slave = pty.openpty()
        resize(slave, 24, 80)
        import subprocess
        proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True, close_fds=True)
        os.close(slave)
    except (ValueError, RuntimeError, OSError) as exc:
        await ws.accept()
        await ws.send_text("Unable to open terminal: " + str(exc))
        await ws.close()
        return
    await ws.accept()
    reader = asyncio.create_task(asyncio.to_thread(os.read, master, 8192))
    receiver = asyncio.create_task(ws.receive_text())
    try:
        while True:
            done, _ = await asyncio.wait((reader, receiver), return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                try:
                    output = reader.result()
                except OSError:
                    break
                if not output:
                    break
                await ws.send_text(output.decode("utf-8", "replace"))
                reader = asyncio.create_task(asyncio.to_thread(os.read, master, 8192))
            if receiver in done:
                message = json.loads(receiver.result())
                if message.get("type") == "input":
                    os.write(master, str(message.get("data", ""))[:8192].encode())
                elif message.get("type") == "resize":
                    resize(master, max(10, min(120, int(message.get("rows", 24)))),
                           max(20, min(300, int(message.get("cols", 80)))))
                receiver = asyncio.create_task(ws.receive_text())
    except (WebSocketDisconnect, ConnectionError, ValueError, KeyError, OSError):
        pass
    finally:
        reader.cancel()
        receiver.cancel()
        try:
            os.close(master)
        except OSError:
            pass
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(proc.wait, timeout=2)
            except Exception:
                os.killpg(proc.pid, signal.SIGKILL)
        try:
            await ws.close()
        except RuntimeError:
            pass
