"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (v, d = 0, unit = "") => (v === null || v === undefined || Number.isNaN(v) ? "--" : Number(v).toFixed(d) + unit);

// ---------------------------------------------------------------- tiny canvas chart
class Chart {
  constructor(canvas, opts = {}) { this.c = canvas; this.o = opts; this.series = []; }
  draw(series, { min = 0, max, refs = [] } = {}) {
    const c = this.c, dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    if (c.width !== w * dpr || c.height !== h * dpr) { c.width = w * dpr; c.height = h * dpr; }
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const all = series.flatMap((s) => s.data.filter((v) => v !== null && v !== undefined));
    if (max === undefined) max = Math.max(10, ...all, ...refs.map((r) => r.v)) * 1.1;
    const L = 34, T = 6, W = w - L - 6, H = h - T - 16;
    const y = (v) => T + H - ((Math.min(Math.max(v, min), max) - min) / (max - min)) * H;
    g.font = "11px system-ui"; g.fillStyle = "#8b98a8"; g.strokeStyle = "#232b36"; g.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const v = min + ((max - min) * i) / 4, yy = Math.round(y(v)) + 0.5;
      g.beginPath(); g.moveTo(L, yy); g.lineTo(L + W, yy); g.stroke();
      g.fillText(v >= 100 ? v.toFixed(0) : v.toFixed(v < 10 ? 1 : 0), 2, yy + 4);
    }
    for (const r of refs) {
      g.strokeStyle = r.color; g.setLineDash([4, 4]);
      g.beginPath(); g.moveTo(L, y(r.v)); g.lineTo(L + W, y(r.v)); g.stroke(); g.setLineDash([]);
    }
    for (const s of series) {
      const n = s.cap || s.data.length; if (s.data.length < 2) continue;
      const off = n - s.data.length;
      g.strokeStyle = s.color; g.lineWidth = 1.6; g.beginPath();
      let pen = false;
      s.data.forEach((v, i) => {
        if (v === null || v === undefined) { pen = false; return; }
        const xx = L + ((i + off) / (n - 1)) * W;
        pen ? g.lineTo(xx, y(v)) : g.moveTo(xx, y(v)); pen = true;
      });
      g.stroke();
    }
    if (this.o.legend) {
      let x = L + 6;
      for (const s of series) {
        g.fillStyle = s.color; g.fillRect(x, T + 4, 8, 8);
        g.fillStyle = "#e6edf3"; g.fillText(s.name, x + 12, T + 12); x += g.measureText(s.name).width + 26;
      }
    }
  }
}

const fpsChart = new Chart($("fpsChart")), ftChart = new Chart($("ftChart")), tChart = new Chart($("tChart"), { legend: true });
const FPS_N = 240, FT_N = 600, T_N = 300;
let fpsHist = [], low1Hist = [], ftHist = [], tHist = {}, last = {}, es = null, serial = null, rec = null, hz = null;

function push(arr, v, n) { arr.push(v); if (arr.length > n) arr.splice(0, arr.length - n); }

function resetCharts() { fpsHist = []; low1Hist = []; ftHist = []; tHist = {}; last = {}; }

// ---------------------------------------------------------------- devices
async function loadDevices(keep = true) {
  const sel = $("deviceSel");
  const prev = keep ? sel.value : null;
  let data;
  try { data = await (await fetch("/api/devices")).json(); } catch { return; }
  sel.innerHTML = "";
  if (data.error) banner(data.error);
  const devs = (data.devices || []);
  if (!devs.length) sel.innerHTML = "<option value=''>No device — plug in USB or connect over Wi-Fi</option>";
  for (const d of devs) {
    const o = document.createElement("option");
    o.value = d.serial; o.disabled = d.state !== "device";
    o.textContent = `${d.transport === "wifi" ? "📶" : "🔌"} ${d.model || d.serial} · ${d.serial}${d.state !== "device" ? " (" + d.state + ")" : ""}`;
    sel.appendChild(o);
  }
  const ok = devs.filter((d) => d.state === "device");
  const pick = ok.find((d) => d.serial === prev) || ok[0];
  if (pick) { sel.value = pick.serial; if (pick.serial !== serial) start(pick.serial); }
  if (devs.some((d) => d.state === "unauthorized")) banner("Phone shows 'unauthorized': unlock it and accept the USB debugging prompt, then press ↻.");
}

function banner(text) { const b = $("banner"); b.textContent = text || ""; b.classList.toggle("hidden", !text); }

function start(s) {
  if (es) es.close();
  serial = s; resetCharts(); banner("");
  if (!s) return;
  es = new EventSource("/api/stream?serial=" + encodeURIComponent(s));
  es.onmessage = (e) => onData(JSON.parse(e.data));
  es.onerror = () => { $("dot").className = "dot err"; };
}

// ---------------------------------------------------------------- render
function cls(el, v, good, warn) {
  el.classList.remove("good", "warn", "bad");
  if (v === null || v === undefined) return;
  el.classList.add(v >= good ? "good" : v >= warn ? "warn" : "bad");
}

function onData(d) {
  if (d.error) { banner(d.error + " — reconnecting…"); $("dot").className = "dot err"; setTimeout(() => loadDevices(), 2500); return; }
  banner(""); $("dot").className = "dot live";
  Object.assign(last, d);
  if (d.refresh) hz = d.refresh;
  const target = hz || 60;

  $("fps").textContent = fmt(d.fps);
  cls($("fps"), d.fps, target * 0.9, target * 0.6);
  $("app").textContent = d.pkg ? d.pkg + (d.layer ? "" : " (no frames)") : "no foreground app";
  $("avg").textContent = fmt(d.avg, 1);
  $("low1").textContent = fmt(d.low1, 1); cls($("low1"), d.low1, target * 0.75, target * 0.5);
  $("low01").textContent = fmt(d.low01, 1); cls($("low01"), d.low01, target * 0.6, target * 0.4);
  $("low1w").textContent = fmt(d.low1_30s, 1);
  $("ft").textContent = fmt(d.frametime, 2, " ms");
  $("jank").textContent = fmt(d.jank_ms, 1, " ms");
  $("stut").textContent = d.stutters_30s ?? "--";
  $("hz").textContent = hz ?? "--";
  $("frames").textContent = d.frames?.toLocaleString() ?? "--";
  $("sess").textContent = d.session_s !== undefined ? `${Math.floor(d.session_s / 60)}:${String(d.session_s % 60).padStart(2, "0")}` : "--";

  push(fpsHist, d.fps, FPS_N); push(low1Hist, d.low1_30s, FPS_N);
  for (const ft of d.new_ft || []) push(ftHist, ft, FT_N);
  fpsChart.draw([{ data: fpsHist, color: "#3ddc97", cap: FPS_N }, { data: low1Hist, color: "#f5b942", cap: FPS_N }],
    { max: Math.max(target, ...fpsHist.filter(Boolean)) * 1.1, refs: [{ v: target, color: "#5aa9ff66" }] });
  ftChart.draw([{ data: ftHist, color: "#b48cff", cap: FT_N }],
    { max: Math.max(1000 / target * 3, Math.min(120, Math.max(...ftHist, 0) * 1.1)), refs: [{ v: 1000 / target, color: "#3ddc9766" }] });

  if (d.temps) renderTemps(d);
  if (d.cpu) renderCpu(d);
  renderMisc();
  if (d.procs) renderProcs(d.procs);
  if (d.device && !$("devInfo").textContent) {
    const v = d.device;
    $("devInfo").textContent = `${v.maker} ${v.model} (${v.codename}) · SoC ${v.soc || "?"} · Android ${v.android} · ${d.serial}`;
  }
  if (rec) recordRow(d);
}

const KEY = ["CPU", "GPU", "SOC", "SKIN", "BATTERY"];
const TCOL = { CPU: "#ff5d5d", GPU: "#b48cff", SOC: "#f5b942", SKIN: "#3ddc97", BATTERY: "#5aa9ff" };
function renderTemps(d) {
  const temps = [...d.temps].sort((a, b) => (KEY.indexOf(a.name) + 1 || 99) - (KEY.indexOf(b.name) + 1 || 99));
  $("temps").innerHTML = temps.map((t) => {
    const c = t.value >= 85 || t.status >= 3 ? "bad" : t.value >= 70 || t.status >= 1 ? "warn" : "";
    return `<div class="temp"><span title="${esc(t.name)}">${esc(t.name)}</span><b class="${c}">${fmt(t.value, 1)}°</b></div>`;
  }).join("") || "<span class='sub'>No temperature sensors readable</span>";
  const st = d.thermal_status;
  const names = ["none", "light", "moderate", "severe", "critical", "emergency", "shutdown"];
  $("tstatus").textContent = st === null || st === undefined ? "" : `throttle status: ${names[st] ?? st}`;
  $("tstatus").className = st >= 2 ? "bad" : st >= 1 ? "warn" : "";
  const show = temps.filter((t) => KEY.includes(t.name)).length ? temps.filter((t) => KEY.includes(t.name)) : temps.slice(0, 4);
  for (const t of show) push((tHist[t.name] ||= []), t.value, T_N);
  tChart.draw(Object.entries(tHist).map(([n, data], i) => ({ name: n, data, cap: T_N, color: TCOL[n] || ["#ff8a5d", "#5dd6ff", "#e0e05d", "#d65dff"][i % 4] })), { min: 20 });
}

function renderCpu(d) {
  $("cpuTot").textContent = d.cpu_total !== null && d.cpu_total !== undefined ? `${d.cpu_total}% total load` : "";
  $("cpus").innerHTML = d.cpu.map((c) => {
    const pct = c.mhz && c.max ? Math.min(100, (c.mhz / c.max) * 100) : 0;
    return `<div class="cpu"><span>${esc(c.id)}</span><div class="bar">${c.usage !== null && c.usage !== undefined ? `<i class="u" style="width:${c.usage}%"></i>` : ""}<i style="width:${pct}%;opacity:.9;height:4px;top:3px"></i></div>
      <em>${c.online ? fmt(c.mhz, 0, " MHz") : "offline"}${c.usage !== null && c.usage !== undefined ? " · " + c.usage + "%" : ""}</em></div>`;
  }).join("") + "<div class='sub'>blue = clock vs max · green = load</div>";
}

function stat(label, val, c = "") { return `<div class="temp"><span>${label}</span><b class="${c}">${val}</b></div>`; }
function renderMisc() {
  const g = last.gpu || {}, b = last.battery || {}, m = last.mem, cool = last.cooling || {};
  const chg = /charg/i.test(b.status || "") && !/dis/i.test(b.status || "");
  $("misc").innerHTML =
    (g.mhz !== undefined || cool.gpu === null || cool.gpu === undefined ? stat("GPU clock", fmt(g.mhz, 0, " MHz")) + stat("GPU load", fmt(g.load, 0, "%")) : "") +
    (cool.gpu !== null && cool.gpu !== undefined ? stat("GPU throttle level", cool.gpu, cool.gpu > 0 ? "warn" : "good") : "") +
    (cool.cpu !== null && cool.cpu !== undefined ? stat("CPU throttle level", cool.cpu, cool.cpu > 0 ? "warn" : "good") : "") +
    stat("Battery", fmt(b.level, 0, "%")) + stat("Status", esc(b.status || "--"), chg ? "warn" : "") +
    stat("Current", fmt(b.ma, 0, " mA")) + stat("Power", fmt(b.w, 2, " W")) +
    stat("Voltage", fmt(b.v, 2, " V")) + stat("Batt temp", fmt(b.temp, 1, "°"), b.temp >= 42 ? "bad" : b.temp >= 38 ? "warn" : "") +
    (m ? stat("RAM free", `${(m.avail_mb / 1024).toFixed(1)} / ${(m.total_mb / 1024).toFixed(1)} GB`) : "");
}

function renderProcs(p) {
  $("procs").querySelector("tbody").innerHTML = p.map((x) =>
    `<tr><td title="${esc(x.name)} (${x.pid}, ${esc(x.user)})">${esc(x.name)}</td><td>${fmt(x.cpu, 1)}</td><td>${fmt(x.mem, 1)}</td></tr>`).join("");
}

// ---------------------------------------------------------------- recording (CSV)
function recordRow(d) {
  const t = Object.fromEntries((last.temps || []).map((x) => [x.name, x.value]));
  const b = last.battery || {}, g = last.gpu || {};
  rec.push([new Date(d.t * 1000).toISOString(), d.pkg, d.fps, d.frametime, d.low1_30s, d.avg, d.low1, d.low01,
    t.CPU, t.GPU, t.SOC, t.SKIN, t.BATTERY, last.thermal_status, last.cpu_total,
    ...(last.cpu || []).map((c) => c.mhz), g.mhz, g.load, b.level, b.ma, b.w, b.status].map((v) => v ?? "").join(","));
}
$("recBtn").onclick = () => {
  if (!rec) { rec = []; $("recBtn").textContent = "■ Stop & save"; $("recBtn").classList.add("on"); return; }
  const cores = (last.cpu || []).map((c) => c.id + "_mhz");
  const head = ["time", "app", "fps", "frametime_ms", "low1_30s", "avg", "low1", "low01", "cpu_c", "gpu_c", "soc_c", "skin_c", "battery_c",
    "thermal_status", "cpu_load", ...cores, "gpu_mhz", "gpu_load", "battery_pct", "battery_ma", "power_w", "charging"].join(",");
  const blob = new Blob([head + "\n" + rec.join("\n") + "\n"], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = `droidfps-${new Date().toISOString().slice(0, 19).replace(/:/g, "")}.csv`; a.click();
  rec = null; $("recBtn").textContent = "● Record"; $("recBtn").classList.remove("on");
};

// ---------------------------------------------------------------- controls
async function post(url, body) {
  $("connMsg").textContent = "…";
  try {
    const r = await (await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    $("connMsg").textContent = (r.ok ? "✔ " : "✖ ") + (r.msg || "");
    return r;
  } catch (e) { $("connMsg").textContent = "✖ " + e; return {}; }
}
$("deviceSel").onchange = (e) => start(e.target.value);
$("refreshBtn").onclick = () => loadDevices();
$("connBtn").onclick = () => { $("connPanel").classList.toggle("hidden"); scanMdns(); };
async function scanMdns() {
  const box = $("mdns"); box.textContent = "Searching the network for phones with Wireless debugging on…";
  let r; try { r = await (await fetch("/api/mdns")).json(); } catch { box.textContent = ""; return; }
  const s = r.services || [];
  if (!s.length) { box.textContent = "No phone found automatically. Open Developer options → Wireless debugging on the phone, or type the IP & port below."; return; }
  box.innerHTML = s.map((x) => `<div class="row" style="align-items:center;margin-bottom:6px"><span>📶 ${esc(x.name)}</span>` +
    (x.connect ? `<button data-c="${esc(x.connect)}">Connect ${esc(x.connect)}</button>` : "") +
    (x.pairing ? `<span class="sub">pairing port ${esc(x.pairing)}</span>` : "") + "</div>").join("");
  box.querySelectorAll("button[data-c]").forEach((b) => b.onclick = async () => {
    const r = await post("/api/connect", { address: b.dataset.c });
    if (r.ok) loadDevices(false);
    else { $("pairAddr").value = (s.find((x) => x.connect === b.dataset.c) || {}).pairing || ""; }
  });
}
$("resetBtn").onclick = () => { if (serial) post("/api/reset", { serial }); resetCharts(); };
$("wifiBtn").onclick = async () => { if (!serial) return; const r = await post("/api/wifi", { serial }); if (r.ok) setTimeout(() => loadDevices(false), 800); };
$("connectBtn").onclick = async () => { const r = await post("/api/connect", { address: $("connAddr").value }); if (r.ok) loadDevices(false); };
$("pairBtn").onclick = () => post("/api/pair", { address: $("pairAddr").value, code: $("pairCode").value });
setInterval(() => { if (!es || es.readyState === 2) loadDevices(); }, 5000);
loadDevices();
