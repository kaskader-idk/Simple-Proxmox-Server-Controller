"use strict";
const $ = (id) => document.getElementById(id);
const state = {host: null, guests: [], filter: "all", selected: null, detail: null,
               tab: "metrics", filePath: "/", socket: null, loading: false, files: []};

function esc(value) {
  return String(value === undefined || value === null ? "—" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function fmt(bytes) {
  if (bytes === undefined || bytes === null) return "—";
  let n = Number(bytes);
  if (!Number.isFinite(n)) return "—";
  const units = ["B", "KiB", "GiB", "TiB", "PiB"];
  if (Math.abs(n) < 1024) return Math.round(n) + " B";
  const units2 = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units2.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 10 ? 1 : 2) + " " + units2[i];
}
function pct(num) { return Math.max(0, Math.min(100, Number(num) || 0)); }
function duration(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const days = Math.floor(s / 86400), hours = Math.floor(s % 86400 / 3600),
        minutes = Math.floor(s % 3600 / 60);
  return (days ? days + "d " : "") + hours + "h " + minutes + "m";
}
function time(ts) { return ts ? new Date(Number(ts) * 1000).toLocaleString() : "—"; }
function $html(id, value) { $(id).innerHTML = value; }
function fact(name, value) {
  return '<div class="fact"><dt>' + esc(name) + '</dt><dd>' + esc(value) + '</dd></div>';
}
function meter(value) { return '<div class="mini-meter"><i style="width:' + pct(value) + '%"></i></div>'; }
function threadGrid(items, topology) {
  if (!items || !items.length) return '<div class="empty">Per-thread data not available inside this guest.</div>';
  return items.map((num, i) => {
    const top = topology && topology[i];
    const label = top ? "CPU " + i + " · Core " + top.core : "CPU " + i;
    return '<div class="thread"><div class="thread-label"><span>' + esc(label) +
      '</span><b>' + pct(num).toFixed(0) + '%</b></div>' + meter(num) + '</div>';
  }).join("");
}
function alertBox(message) {
  const el = $("alert");
  if (!message) { el.classList.add("hidden"); el.textContent = ""; return; }
  el.textContent = message;
  el.classList.remove("hidden");
}
async function api(url, options) {
  const response = await fetch(url, options || {});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.error || data.detail || ("HTTP " + response.status));
  return data;
}
function post(url, data) {
  return api(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(data)});
}
function setBar(id, percent) { $(id).style.width = pct(percent) + "%"; }

function renderHost(host) {
  $("sideNode").textContent = host.node;
  $("welcome").textContent = host.node + " overview";
  $("subtitle").textContent = host.cpu_name || "Proxmox host";
  $("hostCpu").textContent = pct(host.cpu_total).toFixed(1) + "%";
  setBar("hostCpuBar", host.cpu_total);
  $("cpuDetail").textContent = host.physical_cores + " cores / " + host.logical_cores + " threads";
  const mem = host.memory || {}, swap = host.swap || {};
  $("hostMem").textContent = fmt(mem.used);
  setBar("hostMemBar", (mem.used / mem.total) * 100);
  $("memDetail").textContent = fmt(mem.used) + " of " + fmt(mem.total) + " allocated";
  $("hostSwap").textContent = fmt(swap.used);
  setBar("hostSwapBar", (swap.used / swap.total) * 100);
  $("swapDetail").textContent = fmt(swap.used) + " of " + fmt(swap.total);
  $("hostUptime").textContent = duration(host.uptime);
  $("uptimeDetail").textContent = "Since last node boot";
  $("cpuName").textContent = host.cpu_freq ? Math.round(host.cpu_freq.current) + " MHz" : "";
  $html("cpuThreads", threadGrid(host.cpu_threads, host.cpu_topology));
  const temp = [];
  Object.entries(host.temperatures || {}).forEach(([type, items]) => {
    items.slice(0, 4).forEach(x => temp.push(x.label + " " + x.current.toFixed(1) + "°C"));
  });
  $html("hardware",
    fact("CPU", host.cpu_name) + fact("Architecture", host.platform) +
    fact("Kernel", host.kernel) + fact("Processor threads", host.logical_cores) +
    fact("GPUs", (host.gpus || []).join(" | ") || "Not detected") +
    fact("Temperatures", temp.join(" | ") || "No sensor data") +
    fact("System load", (host.load_average || []).map(n=>n.toFixed(2)).join(" · ")) +
    fact("Proxmox tools", host.proxmox ? "Available" : "Not installed"));
  $html("disks", (host.disks || []).length ? host.disks.map(d => {
    const percentage = d.total ? d.used * 100 / d.total : 0;
    return '<div class="volume"><div class="volume-top"><span class="volume-name">' +
      esc(d.mount) + '</span><span class="muted small">' + fmt(d.used) + ' / ' +
      fmt(d.total) + '</span></div>' + meter(percentage) +
      '<div class="volume-bottom">' + esc(d.device) + ' · ' + esc(d.filesystem) +
      ' · ' + percentage.toFixed(1) + '% used</div></div>';
  }).join("") : '<div class="empty">No mounted storage data.</div>');
  $html("networks", (host.networks || []).length ? host.networks.map(n =>
    '<div class="interface"><div class="interface-top"><span class="interface-name">' +
    esc(n.name) + '</span><span class="status ' + (n.up ? "" : "stopped") + '">' +
    (n.up ? "Up" : "Down") + '</span></div><div class="interface-bottom">↑ ' +
    fmt(n.sent) + ' · ↓ ' + fmt(n.received) + ' · ' +
    (n.speed > 0 ? n.speed + " Mbps" : "speed unavailable") + '</div></div>').join("")
    : '<div class="empty">No network data.</div>');
  $html("processes", (host.processes || []).map(p =>
    '<div class="interface"><div class="interface-top"><span class="interface-name">' +
    esc(p.name) + ' <span class="muted">#' + esc(p.pid) + '</span></span><span>' +
    Number(p.memory_percent || 0).toFixed(2) + '% RAM</span></div></div>').join(""));
}
function renderGuests() {
  $("lxcCount").textContent = state.guests.filter(g=>g.kind==="lxc").length;
  $("qemuCount").textContent = state.guests.filter(g=>g.kind==="qemu").length;
  const q = $("search").value.trim().toLowerCase();
  const filtered = state.guests.filter(g => (state.filter === "all" || g.kind === state.filter)
    && (!q || String(g.vmid).includes(q) || String(g.name).toLowerCase().includes(q)));
  $("workloadCount").textContent = filtered.length;
  $html("guests", filtered.length ? filtered.map(g => {
    const memory = g.max_mem ? 100 * g.used_mem / g.max_mem : 0;
    const disk = g.max_disk ? 100 * g.used_disk / g.max_disk : 0;
    const swap = g.max_swap ? 100 * g.used_swap / g.max_swap : 0;
    return '<button class="guest-card" data-kind="' + esc(g.kind) +
      '" data-id="' + esc(g.vmid) + '"><div class="guest-card-head"><span class="guest-title">' +
      esc(g.name) + '</span><span class="status ' +
      (g.status === "running" ? "" : "stopped") + '">' + esc(g.status) +
      '</span></div><div class="guest-meta">' + (g.kind === "lxc" ? "LXC CONTAINER" : "QEMU VIRTUAL MACHINE") +
      ' · ID ' + g.vmid + ' · ' + duration(g.uptime) + '</div><div class="guest-stats">' +
      guestStat("CPU", (Number(g.cpu) * 100).toFixed(1) + "%", g.cpu * 100) +
      guestStat("RAM", fmt(g.used_mem) + " / " + fmt(g.max_mem), memory) +
      guestStat("Swap", fmt(g.used_swap) + " / " + fmt(g.max_swap), swap) +
      guestStat("Disk", fmt(g.used_disk) + " / " + fmt(g.max_disk), disk) +
      '</div></button>';
  }).join("") : '<div class="empty">No matching containers or VMs.</div>');
}
function guestStat(label, value, percent) {
  return '<div><div class="guest-stat-label"><span>' + esc(label) + '</span><strong>' +
    esc(value) + '</strong></div>' + meter(percent) + '</div>';
}
async function refresh() {
  if (state.loading) return;
  state.loading = true;
  try {
    const results = await Promise.allSettled([api("/api/node"), api("/api/guests")]);
    if (results[0].status === "fulfilled") {
      state.host = results[0].value;
      renderHost(state.host);
    }
    if (results[1].status === "fulfilled") {
      state.guests = results[1].value;
      renderGuests();
    }
    const errors = results.filter(x=>x.status === "rejected").map(x=>x.reason.message);
    alertBox(errors.join(" | "));
    $("lastUpdate").textContent = "Updated " + new Date().toLocaleTimeString();
  } catch(e) { alertBox(e.message); }
  finally { state.loading = false; }
}
function showOverview() {
  closeTerminal();
  state.selected = null;
  $("overview").classList.remove("hidden");
  $("detailView").classList.add("hidden");
  $("pageLocation").textContent = state.filter === "all" ? "Overview" :
    state.filter === "lxc" ? "Containers" : "Virtual machines";
}
async function openGuest(kind, vmid) {
  closeTerminal();
  state.selected = {kind, vmid};
  $("overview").classList.add("hidden");
  $("detailView").classList.remove("hidden");
  $("pageLocation").textContent = kind.toUpperCase() + " / " + vmid;
  $("guestName").textContent = "Loading " + vmid + "…";
  state.detail = null;
  selectTab("metrics");
  alertBox("");
  window.scrollTo(0, 0);
  try {
    const detail = await api("/api/guest/" + kind + "/" + vmid);
    if (!state.selected || state.selected.vmid !== vmid || state.selected.kind !== kind) return;
    state.detail = detail;
    renderDetail(detail);
  } catch(e) { alertBox(e.message); }
}
function detailBase() {
  const g = state.selected;
  return "/api/guest/" + g.kind + "/" + g.vmid;
}
function metric(label, value, sub, percent) {
  return '<div class="metric-card"><div class="metric-head">' + esc(label) + '</div>' +
    '<div class="metric-value">' + esc(value) + '</div>' +
    (percent === null ? "" : '<div class="meter"><i style="width:' + pct(percent) + '%"></i></div>') +
    '<div class="metric-foot">' + esc(sub) + '</div></div>';
}
function renderDetail(d) {
  const s = d.raw_status || {}, r = d.reserved || {}, g = d.guest;
  $("guestType").textContent = (d.kind === "lxc" ? "LXC CONTAINER" : "VIRTUAL MACHINE") + " · " + d.vmid;
  $("guestName").textContent = d.name;
  $("guestSub").textContent = "Proxmox node " + d.node + " · VMID " + d.vmid;
  $("guestStatus").textContent = d.status || "unknown";
  $("guestStatus").className = "status " + (d.status === "running" ? "" : "stopped");
  $("startGuest").disabled = d.status === "running";
  ["shutdownGuest", "rebootGuest", "stopGuest"].forEach(id=>$(id).disabled = d.status !== "running");
  const useMem = g ? g.memory_used : Number(s.mem || 0);
  const maxMem = r.memory || Number(s.maxmem || 0);
  const useSwap = g ? g.swap_used : Number(s.swap || 0);
  const maxSwap = g ? g.swap_total : (r.swap || Number(s.maxswap || 0));
  const maxDisk = (r.disks || []).reduce((sum, disk)=>sum + disk.reserved_bytes, 0)
    || Number(s.maxdisk || 0);
  const useDisk = g ? g.disk_used : Number(s.disk || 0);
  $html("guestMetrics",
    metric("CPU USAGE", ((Number(s.cpu) || 0) * 100).toFixed(1) + "%",
      r.cpu_threads + " assigned threads", Number(s.cpu) * 100) +
    metric("MEMORY", fmt(useMem), fmt(useMem) + " / " + fmt(maxMem),
      maxMem ? useMem * 100 / maxMem : 0) +
    metric("SWAP", fmt(useSwap), fmt(useSwap) + " / " + fmt(maxSwap),
      maxSwap ? useSwap * 100 / maxSwap : 0) +
    metric("DISK USAGE", fmt(useDisk), fmt(useDisk) + " / " + fmt(maxDisk),
      maxDisk ? useDisk * 100 / maxDisk : 0));
  $html("guestThreads", threadGrid(g && g.cpu_threads, null));
  $html("guestFacts", g ?
    fact("Hostname", g.hostname) + fact("Guest OS", g.os) + fact("Kernel", g.kernel) +
    fact("Architecture", g.architecture) + fact("Guest RAM", fmt(g.memory_total)) +
    fact("Guest swap", fmt(g.swap_total)) +
    fact("Root filesystem", fmt(g.disk_used) + " / " + fmt(g.disk_total)) +
    fact("CPU threads observed", (g.cpu_threads || []).length)
    : '<div class="empty">' + esc(d.guest_error || "No live guest metrics available") + '</div>');
  $html("guestDisks", (r.disks || []).length ? r.disks.map(x =>
    '<div class="interface"><div class="interface-top"><strong>' + esc(x.device) +
    '</strong><span>' + fmt(x.reserved_bytes) + ' reserved</span></div>' +
    '<div class="interface-bottom">' + esc(x.configuration) + '</div></div>').join("")
    : '<div class="empty">No configured disk data.</div>');
  $html("guestConfig", Object.entries(d.config || {}).sort().map(([k,v]) =>
    fact(k, typeof v === "object" ? JSON.stringify(v) : v)).join(""));
}
function selectTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach(b=>b.classList.toggle("active",b.dataset.tab === tab));
  ["metrics","files","terminal"].forEach(name => {
    $(name + "Tab").classList.toggle("hidden", name !== tab);
  });
  if (tab !== "terminal") closeTerminal();
  if (!state.selected || !state.detail) return;
  if (tab === "files") listFiles(state.filePath);
  if (tab === "terminal") connectTerminal();
}
function normalizePath(path) {
  const parts = [];
  String(path || "/").split("/").forEach(part => {
    if (!part || part === ".") return;
    if (part === "..") parts.pop();
    else parts.push(part);
  });
  return "/" + parts.join("/");
}
function joinPath(parent, name) { return normalizePath(parent + "/" + name); }
function fileHref(path) { return detailBase() + "/files/download?path=" + encodeURIComponent(path); }
async function listFiles(path) {
  if (!state.selected) return;
  state.filePath = normalizePath(path);
  $("filePath").value = state.filePath;
  $html("fileList", '<div class="empty">Loading files…</div>');
  try {
    const data = await api(detailBase() + "/files?path=" + encodeURIComponent(state.filePath));
    if (data.path !== state.filePath) return;
    state.files = data.items || [];
    $html("fileList", state.files.length ? state.files.map(file => {
      const icon = file.directory ? "📁" : file.link ? "🔗" : "📄";
      return '<div class="file-row" draggable="true" data-path="' + esc(file.path) +
        '" data-dir="' + file.directory + '"><div class="file-name" data-open="' +
        esc(file.path) + '" title="Open or download"><span class="file-symbol">' +
        icon + '</span><span>' + esc(file.name) + '</span></div><span class="muted">' +
        (file.directory ? "Folder" : fmt(file.size)) + '</span><span class="file-modified muted">' +
        esc(time(file.modified)) + '</span><div class="file-row-actions">' +
        '<button class="icon-btn" data-op="rename" title="Rename">Rename</button>' +
        '<button class="icon-btn" data-op="more" title="Copy, move, archive, extract">More</button>' +
        '<button class="icon-btn" data-op="delete" title="Delete">Delete</button>' +
        '</div></div>';
    }).join("") : '<div class="empty">This folder is empty. Drop files to upload.</div>');
    const filesystem = data.filesystem || {};
    $("fileInfo").textContent = state.files.length + " items" +
      (data.limited ? " · first 2000 shown" : "") + " · Free: " + fmt(filesystem.free) +
      " / " + fmt(filesystem.size);
  } catch(e) {
    $html("fileList", '<div class="empty">' + esc(e.message) + '</div>');
    $("fileInfo").textContent = "Guest must be running. Linux guests need Python 3; VMs also need QEMU Guest Agent.";
  }
}
async function performAction(op, path, destination, format) {
  await post(detailBase() + "/files/action",
    {op, path, destination:destination || "", format:format || "zip"});
  await listFiles(state.filePath);
}
async function manageFile(op, path) {
  const file = state.files.find(f=>f.path === path);
  if (!file) return;
  try {
    if (op === "delete") {
      if (!confirm("Permanently delete " + file.name + (file.directory ? " and all its contents" : "") + "?")) return;
      await performAction("delete", path);
    } else if (op === "rename") {
      const name = prompt("New file name", file.name);
      if (!name || name === file.name || name.includes("/") || name === "." || name === "..") return;
      await performAction("move", path, joinPath(state.filePath, name));
    } else if (op === "more") {
      const choice = prompt("Action: copy, move, zip, targz, extract, download" +
        (file.directory ? " (folders cannot be downloaded directly)" : ""), "copy");
      if (!choice) return;
      const mode = choice.toLowerCase().trim();
      if (mode === "download" && !file.directory) {
        window.location.href = fileHref(path);
      } else if (mode === "copy" || mode === "move") {
        const dest = prompt("Destination absolute path", path);
        if (dest && normalizePath(dest) !== path)
          await performAction(mode, path, normalizePath(dest));
      } else if (mode === "zip" || mode === "targz") {
        const ext = mode === "zip" ? ".zip" : ".tar.gz";
        const dest = prompt("Archive destination", path + ext);
        if (dest) await performAction("compress", path, normalizePath(dest),
          mode === "zip" ? "zip" : "tar.gz");
      } else if (mode === "extract") {
        const dest = prompt("Extract into directory", state.filePath);
        if (dest) await performAction("extract", path, normalizePath(dest));
      } else {
        alertBox("Unknown action");
      }
    }
  } catch(e) { alertBox(e.message); }
}
async function uploadFiles(files) {
  if (!files || !files.length || !state.selected) return;
  alertBox("");
  for (const file of files) {
    const form = new FormData();
    form.append("file", file, file.name);
    try {
      $("fileInfo").textContent = "Uploading " + file.name + " (" + fmt(file.size) + ")…";
      await api(detailBase() + "/files/upload?path=" + encodeURIComponent(state.filePath),
                {method: "POST", body: form});
    } catch(e) { alertBox("Upload of " + file.name + ": " + e.message); }
  }
  await listFiles(state.filePath);
}
function stripAnsi(text) {
  return text.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/\x1b\][^\x07]*(\x07|\x1b\\)/g, "");
}
function closeTerminal() {
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
    state.socket = null;
  }
  $("terminalStatus").textContent = "Disconnected";
}
function sendTerminal(text) {
  if (state.socket && state.socket.readyState === WebSocket.OPEN)
    state.socket.send(JSON.stringify({type:"input", data:text}));
}
function connectTerminal() {
  closeTerminal();
  if (!state.selected) return;
  $("terminalOutput").textContent = "";
  $("terminalStatus").textContent = "Connecting…";
  const schema = location.protocol === "https:" ? "wss://" : "ws://";
  const socket = new WebSocket(schema + location.host + "/ws/" +
    state.selected.kind + "/" + state.selected.vmid + "/terminal");
  state.socket = socket;
  socket.onopen = () => {
    $("terminalStatus").textContent = "Connected";
    $("terminalInput").focus();
    socket.send(JSON.stringify({type:"resize",rows:25,cols:100}));
  };
  socket.onmessage = e => {
    const output = $("terminalOutput");
    output.textContent = (output.textContent + stripAnsi(e.data)).slice(-90000);
    output.scrollTop = output.scrollHeight;
  };
  socket.onerror = () => { $("terminalStatus").textContent = "Console error"; };
  socket.onclose = () => {
    if (state.socket === socket) {
      $("terminalStatus").textContent = "Disconnected";
      state.socket = null;
    }
  };
}
document.querySelectorAll(".nav").forEach(button => button.addEventListener("click", () => {
  state.filter = button.dataset.filter;
  document.querySelectorAll(".nav").forEach(b => b.classList.toggle("active", b === button));
  renderGuests();
  showOverview();
  $("guests").scrollIntoView({behavior:"smooth",block:"nearest"});
}));
$("refresh").addEventListener("click", refresh);
$("search").addEventListener("input", renderGuests);
$("guests").addEventListener("click", ev => {
  const button = ev.target.closest(".guest-card");
  if (button) {
    state.filePath = "/";
    openGuest(button.dataset.kind, Number(button.dataset.id));
  }
});
$("back").addEventListener("click", showOverview);
document.querySelectorAll(".tab").forEach(button=>
  button.addEventListener("click",()=>selectTab(button.dataset.tab)));
[["startGuest","start"],["shutdownGuest","shutdown"],["rebootGuest","reboot"],["stopGuest","stop"]]
  .forEach(([id,action]) => $(id).addEventListener("click", async () => {
    if (!state.selected) return;
    if (action === "stop" && !confirm("Force stop guest immediately? Unsaved data may be lost.")) return;
    try {
      await post(detailBase() + "/power/" + action, {});
      await new Promise(resolve => setTimeout(resolve, 1000));
      const {kind,vmid} = state.selected;
      await openGuest(kind,vmid);
      await refresh();
    } catch(e) { alertBox(e.message); }
  }));
$("fileRefresh").addEventListener("click",()=>listFiles(state.filePath));
$("fileUp").addEventListener("click",()=>listFiles(normalizePath(state.filePath + "/..")));
$("fileGo").addEventListener("click",()=>listFiles($("filePath").value));
$("filePath").addEventListener("keydown",e=>{if(e.key==="Enter")listFiles(e.target.value);});
$("newFolder").addEventListener("click",async()=>{
  const name = prompt("New folder name");
  if (!name || name.includes("/") || name === "." || name === "..") return;
  try { await performAction("mkdir",joinPath(state.filePath,name)); }
  catch(e) { alertBox(e.message); }
});
$("uploadInput").addEventListener("change",async e=>{
  await uploadFiles(Array.from(e.target.files)); e.target.value = "";
});
$("fileList").addEventListener("click",e=>{
  const row = e.target.closest(".file-row");
  if (!row) return;
  const op = e.target.closest("[data-op]");
  if (op) return manageFile(op.dataset.op,row.dataset.path);
  const name = e.target.closest("[data-open]");
  if (name) {
    const file = state.files.find(f=>f.path === row.dataset.path);
    if (file && file.directory) listFiles(file.path);
    else window.location.href = fileHref(row.dataset.path);
  }
});
$("fileList").addEventListener("dragstart",e=>{
  const row = e.target.closest(".file-row");
  if (!row) return;
  const file = state.files.find(f=>f.path === row.dataset.path);
  if (!file) return;
  e.dataTransfer.setData("text/plain", file.path);
  if (!file.directory) {
    e.dataTransfer.setData("DownloadURL", "application/octet-stream:" + file.name +
      ":" + new URL(fileHref(file.path),location.href).href);
  }
});
const zone = $("dropzone");
zone.addEventListener("dragover",e=>{e.preventDefault();zone.classList.add("dragover");});
zone.addEventListener("dragleave",()=>zone.classList.remove("dragover"));
zone.addEventListener("drop",async e=>{
  e.preventDefault();zone.classList.remove("dragover");
  if (e.dataTransfer.files.length) return uploadFiles(Array.from(e.dataTransfer.files));
  const source = e.dataTransfer.getData("text/plain");
  const row = e.target.closest(".file-row");
  const target = row && row.dataset.dir === "true" ? row.dataset.path : state.filePath;
  if (!source || !source.startsWith("/") || source === target) return;
  const dest = joinPath(target, source.split("/").pop());
  if (dest === source) return;
  if (!confirm("Move " + source + " to " + dest + "?")) return;
  try { await performAction("move",source,dest); }
  catch(err) { alertBox(err.message); }
});
$("terminalOutput").addEventListener("click",()=>$("terminalInput").focus());
$("reconnectTerminal").addEventListener("click",connectTerminal);
$("terminalInput").addEventListener("keydown",e=>{
  let data = null;
  if (e.ctrlKey && e.key.toLowerCase() === "c") data = "\x03";
  else if (e.key === "Enter") data = "\r";
  else if (e.key === "Backspace") data = "\x7f";
  else if (e.key === "Tab") data = "\t";
  else if (e.key === "ArrowUp") data = "\x1b[A";
  else if (e.key === "ArrowDown") data = "\x1b[B";
  else if (e.key === "ArrowRight") data = "\x1b[C";
  else if (e.key === "ArrowLeft") data = "\x1b[D";
  else if (e.key.length === 1 && !e.altKey && !e.metaKey) data = e.key;
  if (data !== null) { e.preventDefault();sendTerminal(data); }
});
$("terminalInput").addEventListener("paste",e=>{
  const text = e.clipboardData.getData("text");
  e.preventDefault();sendTerminal(text);
});
$("commandForm").addEventListener("submit",async e=>{
  e.preventDefault();
  if (!state.selected) return;
  $("commandOutput").textContent = "Running…";
  try {
    const output = await post(detailBase() + "/command",{command:$("commandInput").value});
    $("commandOutput").textContent = (output.output || "(no output)") + "\nExit code: " + output.exitcode;
  } catch(err) { $("commandOutput").textContent = err.message; }
});
refresh();
setInterval(refresh, 10000);
