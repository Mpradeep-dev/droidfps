#!/usr/bin/env python3
"""DroidFPS - live Android performance monitor over ADB (USB or Wi-Fi).

Reads FPS, frame times, 1% / 0.1% lows, temperatures, CPU/GPU clocks,
battery power and top processes from a phone with USB debugging enabled,
and serves a live dashboard at http://127.0.0.1:8765.

Standard library only. Needs `adb` (platform-tools) in PATH.
"""
import argparse
import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
ADB = os.environ.get("ADB", "adb")
ADDR_RE = re.compile(r"^[A-Za-z0-9._\-:\[\]]{1,120}$")
INTERVAL = 0.5  # seconds between samples; SurfaceFlinger keeps only ~128 frames
LIGHT_INTERVAL = 0.8  # still under the ~1 s that 128 frames last at 120 Hz

GPU_CANDIDATES = [
    "/sys/kernel/ged/hal/current_freq",
    "/sys/kernel/ged/hal/gpu_utilization",
    "/sys/class/kgsl/kgsl-3d0/gpuclk",
    "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage",
    "/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq",
    "/sys/class/devfreq/*mali*/cur_freq",
    "/sys/class/devfreq/*gpu*/cur_freq",
    "/sys/class/misc/mali0/device/utilization",
]


# --------------------------------------------------------------------------- adb helpers

def adb(*args, timeout=25):
    try:
        p = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "adb not found in PATH (install Android platform-tools)"
    except subprocess.TimeoutExpired:
        return 124, "adb timed out"


def list_devices():
    code, out = adb("devices", "-l")
    if code == 127:
        return {"error": out, "devices": []}
    devs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or line.startswith(("List of", "*")):
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        serial = parts[0]
        wifi = ":" in serial or "._adb-tls-connect" in serial
        devs.append({
            "serial": serial,
            "state": parts[1],
            "model": info.get("model", "").replace("_", " "),
            "transport": "wifi" if wifi else "usb",
        })
    return {"devices": devs}


class Shell:
    """One persistent `adb shell`; each call is a script ended by a unique marker."""

    def __init__(self, serial):
        self.p = subprocess.Popen(
            [ADB, "-s", serial, "shell"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace",
        )
        self.lock = threading.Lock()

    def run(self, script):
        marker = "__DFPS_%s__" % uuid.uuid4().hex[:10]
        with self.lock:
            try:
                self.p.stdin.write(script + "\necho " + marker + "\n")
                self.p.stdin.flush()
            except (BrokenPipeError, OSError):
                raise EOFError("device disconnected")
            lines = []
            while True:
                line = self.p.stdout.readline()
                if not line:
                    raise EOFError("device disconnected")
                line = line.rstrip("\r\n")
                if line == marker:
                    return lines
                lines.append(line)

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        self.p.kill()


def sections(lines):
    out, cur = {}, None
    for l in lines:
        if l.startswith("@@"):
            cur = l[2:]
            out[cur] = []
        elif cur:
            out[cur].append(l)
    return out


def first_int(s):
    m = re.search(r"-?\d+", s or "")
    return int(m.group()) if m else None


def lows(fts):
    """avg fps, 1% low, 0.1% low (average fps of the slowest 1% / 0.1% of frames)."""
    n = len(fts)
    if n < 2:
        return None, None, None
    s = sorted(fts, reverse=True)
    k1, k01 = max(1, n // 100), max(1, n // 1000)
    return 1000 * n / sum(s), 1000 * k1 / sum(s[:k1]), 1000 * k01 / sum(s[:k01])


def r(v, d=1):
    return None if v is None else round(v, d)


# --------------------------------------------------------------------------- sampler

class Sampler(threading.Thread):
    def __init__(self, serial, manager):
        super().__init__(daemon=True)
        self.serial, self.manager = serial, manager
        self.subs, self.sub_lock = set(), threading.Lock()
        self.idle_since = time.time()
        self.stop_evt = threading.Event()
        self.reset()
        self.pkg = None
        self.layer, self.candidates, self.evaluate = None, [], False
        self.gpu_paths, self.cpu_max, self.device = [], {}, {}
        self.prev_stat = None
        self.light = False  # light mode: no process list, fewer and slower polls
        self.thermal_zones = False
        self.last_frame_host = 0.0

    def reset(self):
        self.window = deque()  # (present_ns, frametime_ms) for the rolling 30 s view
        self.session = deque(maxlen=600_000)
        self.last_ts = 0
        self.started = time.time()

    # subscribers ----------------------------------------------------------
    def subscribe(self):
        q = queue.Queue(maxsize=50)
        with self.sub_lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.sub_lock:
            self.subs.discard(q)
            if not self.subs:
                self.idle_since = time.time()

    def broadcast(self, obj):
        data = obj if obj is None else json.dumps(obj, separators=(",", ":")).encode()
        with self.sub_lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    # main loop --------------------------------------------------------------
    def run(self):
        sh = None
        try:
            sh = self.sh = Shell(self.serial)
            self.probe()
            tick = 0
            while not self.stop_evt.is_set():
                t0 = time.time()
                self.step(tick)
                tick += 1
                with self.sub_lock:
                    idle = not self.subs and time.time() - self.idle_since > 10
                if idle:
                    break
                time.sleep(max(0.05, (LIGHT_INTERVAL if self.light else INTERVAL) - (time.time() - t0)))
        except EOFError as e:
            self.broadcast({"error": str(e)})
        except Exception as e:  # keep the server alive whatever the phone returns
            self.broadcast({"error": "sampler error: %s" % e})
        finally:
            if sh:
                sh.close()
            self.manager.forget(self)
            self.broadcast(None)

    def probe(self):
        gpu = " ".join(GPU_CANDIDATES)
        sec = sections(self.sh.run(
            "echo @@props\n"
            "getprop ro.product.manufacturer; getprop ro.product.model; getprop ro.soc.model;"
            " getprop ro.build.version.release; getprop ro.product.device\n"
            "echo @@gpu\n"
            f"for f in {gpu}; do [ -r \"$f\" ] && v=$(head -c 120 \"$f\" 2>/dev/null | tr '\\n' ' ') "
            "&& [ -n \"$v\" ] && echo \"$f|$v\"; done\n"
            "echo @@cpumax\n"
            "for c in /sys/devices/system/cpu/cpu[0-9]*; do echo ${c##*/} $(cat $c/cpufreq/cpuinfo_max_freq 2>/dev/null); done\n"
            "echo @@thermal\n"
            "dumpsys thermalservice | grep -c 'Temperature{'"
        ))
        p = sec.get("props", []) + [""] * 5
        self.device = {"maker": p[0], "model": p[1], "soc": p[2], "android": p[3], "codename": p[4]}
        self.gpu_paths = [l.split("|", 1)[0] for l in sec.get("gpu", []) if "|" in l]
        for l in sec.get("cpumax", []):
            parts = l.split()
            if len(parts) == 2 and parts[1].isdigit():
                self.cpu_max[parts[0]] = int(parts[1]) // 1000
        self.thermal_zones = (first_int(" ".join(sec.get("thermal", []))) or 0) == 0

    def step(self, tick):
        parts = []
        if self.evaluate and self.candidates:
            for i, l in enumerate(self.candidates[:4]):
                parts.append(("lat%d" % i, "dumpsys SurfaceFlinger --latency %s" % shlex.quote(l)))
        elif self.layer:
            parts.append(("lat", "dumpsys SurfaceFlinger --latency %s" % shlex.quote(self.layer)))
        L = self.light
        slow = tick % (4 if L else 2) == 0
        if slow:
            if self.thermal_zones:
                parts.append(("zones", "for z in /sys/class/thermal/thermal_zone*; do echo \"$(cat $z/type 2>/dev/null)|$(cat $z/temp 2>/dev/null)\"; done"))
            else:
                parts.append(("thermal", "dumpsys thermalservice | sed -n '/Thermal Status/p;/Current temperatures from HAL/,$p'"))
            parts.append(("cpu", "for c in /sys/devices/system/cpu/cpu[0-9]*; do echo ${c##*/} $(cat $c/cpufreq/scaling_cur_freq 2>/dev/null) $(cat $c/online 2>/dev/null); done"))
            parts.append(("stat", "grep '^cpu' /proc/stat 2>/dev/null"))
            parts.append(("bat", "dumpsys battery | grep -E '^ +(level|status|voltage|temperature|AC powered|USB powered|Wireless powered):';"
                          " for f in /sys/class/power_supply/battery/current_now /sys/class/power_supply/*/current_now; do"
                          " v=$(cat $f 2>/dev/null) && [ -n \"$v\" ] && echo \"current_now: $v\" && break; done"))
            if self.gpu_paths:
                parts.append(("gpu", "for f in %s; do echo \"$f|$(head -c 120 $f 2>/dev/null | tr '\\n' ' ')\"; done" % " ".join(self.gpu_paths)))
        if not L and tick % 4 == 0:
            parts.append(("top", "top -b -n 1 -m 8 -o PID,USER,%CPU,%MEM,NAME -s 3"))
        if not L and tick % 4 == 1 or L and tick % 16 == 2:
            parts.append(("mem", "grep -E 'MemTotal|MemAvailable' /proc/meminfo"))
        if tick % (8 if L else 6) == 0:
            parts.append(("focus", "dumpsys activity activities | grep -m1 -E 'topResumedActivity|mResumedActivity'"))
            parts.append(("layers", "dumpsys SurfaceFlinger --list"))
        if tick % 20 == 0:
            parts.append(("refresh", "dumpsys SurfaceFlinger | grep -m1 -E 'refresh-rate'"))

        sec = sections(self.sh.run("\n".join("echo @@%s\n%s" % p for p in parts)))
        msg = {"t": time.time(), "serial": self.serial, "device": self.device, "light": self.light}

        if "focus" in sec:
            self.update_target(sec)
        new_ft = self.update_frames(sec)
        msg.update(self.frame_stats(new_ft))
        msg["pkg"], msg["layer"] = self.pkg, self.layer

        if "thermal" in sec:
            msg.update(self.parse_thermal(sec["thermal"]))
        if "zones" in sec:
            msg.update(self.parse_zones(sec["zones"]))
        if "cpu" in sec:
            msg["cpu"], msg["cpu_total"] = self.parse_cpu(sec["cpu"], sec.get("stat", []))
        if "bat" in sec:
            msg["battery"] = self.parse_battery(sec["bat"])
        if "gpu" in sec:
            msg["gpu"] = self.parse_gpu(sec["gpu"])
        if "top" in sec:
            msg["procs"], tot = self.parse_top(sec["top"])
            if tot is not None and msg.get("cpu_total") is None and "stat" not in sec:
                msg["cpu_total"] = tot
        if "mem" in sec:
            m = {k: first_int(v) for k, v in (l.split(":", 1) for l in sec["mem"] if ":" in l)}
            if m.get("MemTotal"):
                msg["mem"] = {"total_mb": m["MemTotal"] // 1024, "avail_mb": (m.get("MemAvailable") or 0) // 1024}
        if "refresh" in sec:
            m = re.search(r"([\d.]+)\s*fps", " ".join(sec["refresh"]))
            if m:
                msg["refresh"] = round(float(m.group(1)))
        self.broadcast(msg)

    # frames ---------------------------------------------------------------
    def update_target(self, sec):
        m = re.search(r"\s([\w.]+)/[\w.$]+", " ".join(sec.get("focus", [])))
        pkg = m.group(1) if m else None
        cands = []
        for l in sec.get("layers", []):
            if not pkg or pkg not in l:
                continue
            name = re.sub(r" parentId=.*", "", l.strip()).replace("RequestedLayerState{", "").rstrip("}")
            if name and name not in cands:
                cands.append(name)
        # Games and video draw into a SurfaceView; prefer it, then BLAST layers.
        cands.sort(key=lambda n: (("SurfaceView" not in n), ("BLAST" not in n), "Background" in n))
        if pkg != self.pkg:
            self.pkg = pkg
            self.reset()
        if cands != self.candidates:
            self.candidates = cands
            self.evaluate = True
            if not cands:
                self.layer = None
        elif self.layer and time.time() - self.last_frame_host > 2 and len(cands) > 1:
            self.evaluate = True  # current layer went quiet, look for the busy one

    @staticmethod
    def present_times(lines):
        ts = []
        for l in lines[1:]:
            p = l.split()
            if len(p) == 3:
                try:
                    v = int(p[1])
                except ValueError:
                    continue
                if 0 < v < 2 ** 62:
                    ts.append(v)
        return sorted(ts)

    def update_frames(self, sec):
        if self.evaluate and any(k.startswith("lat") and k != "lat" for k in sec):
            best, best_n, best_ts = None, -1, []
            for i, name in enumerate(self.candidates[:4]):
                ts = self.present_times(sec.get("lat%d" % i, []))
                n = len([t for t in ts if ts and t > ts[-1] - 1_000_000_000]) if ts else 0
                if ts and n > best_n:
                    best, best_n, best_ts = name, n, ts
            self.evaluate = False
            if best is None:
                return []
            if best != self.layer:
                self.layer = best
                self.last_ts = 0
            ts = best_ts
        elif "lat" in sec:
            ts = self.present_times(sec["lat"])
        else:
            return []
        new = [t for t in ts if t > self.last_ts]
        if not new:
            return []
        # If the oldest buffered frame is already newer than our last one, frames were
        # dropped from SurfaceFlinger's ring buffer: don't count that gap as a stutter.
        prev = self.last_ts if self.last_ts and ts[0] <= self.last_ts else 0
        out = []
        for t in new:
            if prev:
                ft = (t - prev) / 1e6
                if 0 < ft < 1000:  # >1 s means the app was idle, not a frame
                    self.window.append((t, ft))
                    self.session.append(ft)
                    out.append(round(ft, 2))
            prev = t
        self.last_ts = new[-1]
        self.last_frame_host = time.time()
        while self.window and self.window[0][0] < self.last_ts - 30_000_000_000:
            self.window.popleft()
        return out

    def frame_stats(self, new_ft):
        if time.time() - self.last_frame_host > 1.5:
            fps = 0.0
            cur_ft = None
        else:
            last1 = [ft for t, ft in self.window if t > self.last_ts - 1_000_000_000]
            fps = 1000 * len(last1) / sum(last1) if last1 else 0.0
            cur_ft = sum(last1) / len(last1) if last1 else None
        w = [ft for _, ft in self.window]
        _, wl1, wl01 = lows(w)
        avg, l1, l01 = lows(self.session)
        med = sorted(w)[len(w) // 2] if w else None
        stutter = sum(1 for ft in w if med and ft > 2 * med and ft > 25)
        return {
            "fps": r(fps), "frametime": r(cur_ft, 2), "new_ft": new_ft,
            "low1_30s": r(wl1), "low01_30s": r(wl01), "stutters_30s": stutter,
            "avg": r(avg), "low1": r(l1), "low01": r(l01),
            "frames": len(self.session), "session_s": round(time.time() - self.started),
            "jank_ms": r(max(w), 1) if w else None,
        }

    # parsers --------------------------------------------------------------
    @staticmethod
    def parse_thermal(lines):
        temps, status, cool = {}, None, {}
        for l in lines:
            m = re.search(r"CoolingDevice\{mValue=(\d+), mType=(\d+), mName=([^}]+)\}", l)
            if m:
                cool[m.group(3)] = int(m.group(1))
                continue
            m = re.search(r"Thermal Status:\s*(\d)", l)
            if m:
                status = int(m.group(1))
            m = re.search(r"mValue=([-\d.]+), mType=(-?\d+), mName=([^,]+), mStatus=(\d+)", l)
            if m:
                temps[m.group(3)] = {"name": m.group(3), "value": round(float(m.group(1)), 1),
                                     "type": int(m.group(2)), "status": int(m.group(4))}
        gpu = next((v for k, v in cool.items() if re.search(r"mali|gpu|kgsl", k, re.I)), None)
        cpu = {k: v for k, v in cool.items() if re.search(r"cpu", k, re.I)}
        out = {"temps": list(temps.values()), "thermal_status": status,
               "cooling": {"gpu": gpu, "cpu": max(cpu.values()) if cpu else None}}
        return out

    @staticmethod
    def parse_zones(lines):
        temps = {}
        for l in lines:
            name, _, v = l.partition("|")
            v = first_int(v)
            if not name or v is None or v <= 0:
                continue
            c = v / 1000 if v > 1000 else float(v)
            if 0 < c < 150 and re.search(r"cpu|gpu|soc|batt|skin|ap|mtktscpu|tsens", name, re.I):
                temps.setdefault(name, {"name": name, "value": round(c, 1), "type": -1, "status": 0})
        return {"temps": list(temps.values())[:16]}

    def parse_cpu(self, lines, stat):
        usage = {}
        cur = {}
        for l in stat:
            p = l.split()
            if len(p) >= 5 and p[0].startswith("cpu"):
                vals = [int(x) for x in p[1:] if x.isdigit()]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                cur[p[0]] = (sum(vals), idle)
        if self.prev_stat:
            for k, (tot, idle) in cur.items():
                if k in self.prev_stat:
                    dt, di = tot - self.prev_stat[k][0], idle - self.prev_stat[k][1]
                    if dt > 0:
                        usage[k] = round(100 * (dt - di) / dt)
        self.prev_stat = cur or None
        cores = []
        for l in lines:
            p = l.split()
            if not p:
                continue
            f = int(p[1]) // 1000 if len(p) > 1 and p[1].isdigit() else None
            online = not (len(p) > 2 and p[2] == "0")
            cores.append({"id": p[0], "mhz": f if online else None, "max": self.cpu_max.get(p[0]),
                          "usage": usage.get(p[0]), "online": online})
        cores.sort(key=lambda c: int(c["id"][3:]))
        return cores, usage.get("cpu")

    @staticmethod
    def parse_battery(lines):
        b = {k.strip(): v.strip() for k, v in (l.split(":", 1) for l in lines if ":" in l)}
        cur, volt = first_int(b.get("current_now")), first_int(b.get("voltage"))
        ma = None if cur is None else (cur / 1000 if abs(cur) > 20000 else cur)
        v = None if volt is None else (volt / 1e6 if volt > 100000 else volt / 1000)
        t = first_int(b.get("temperature"))
        st = {"1": "Unknown", "2": "Charging", "3": "Discharging", "4": "Not charging", "5": "Full"}.get(b.get("status"), b.get("status"))
        plugged = any(b.get(k) == "true" for k in ("AC powered", "USB powered", "Wireless powered"))
        if st == "Not charging" and not plugged:
            st = "Discharging"
        return {"level": first_int(b.get("level")), "status": st or None, "plugged": plugged,
                "ma": r(ma, 0), "v": r(v, 3), "w": r(abs(ma) / 1000 * v, 2) if ma is not None and v else None,
                "temp": r(t / 10) if t is not None else None}

    @staticmethod
    def parse_gpu(lines):
        g = {}
        for l in lines:
            path, _, val = l.partition("|")
            n = first_int(val)
            if n is None:
                continue
            if re.search(r"util|busy", path):
                g.setdefault("load", max(0, min(100, n)))
            else:
                mhz = n / 1e6 if n > 10_000_000 else n / 1000 if n > 10_000 else n
                g.setdefault("mhz", round(mhz))
        return g

    @staticmethod
    def parse_top(lines):
        procs, tot, hdr = [], None, False
        for l in lines:
            m = re.search(r"(\d+)%cpu.*?(\d+)%idle", l)
            if m and int(m.group(1)):
                tot = round(100 * (int(m.group(1)) - int(m.group(2))) / int(m.group(1)))
            if re.match(r"\s*PID\s", l):
                hdr = True
                continue
            if hdr:
                p = l.split(None, 4)
                if len(p) == 5 and p[0].isdigit():
                    try:
                        procs.append({"pid": int(p[0]), "user": p[1], "cpu": float(p[2]),
                                      "mem": float(p[3]), "name": p[4].strip()})
                    except ValueError:
                        pass
        return procs, tot


class Manager:
    def __init__(self):
        self.lock = threading.Lock()
        self.samplers = {}

    def get(self, serial):
        with self.lock:
            s = self.samplers.get(serial)
            if s is None or not s.is_alive():
                s = Sampler(serial, self)
                self.samplers[serial] = s
                s.start()
            return s

    def forget(self, s):
        with self.lock:
            if self.samplers.get(s.serial) is s:
                del self.samplers[s.serial]


MANAGER = Manager()


# --------------------------------------------------------------------------- wifi helpers

def mdns_services():
    """Phones advertising Wireless debugging on this network (connect and pairing ports)."""
    _, out = adb("mdns", "services", timeout=8)
    found = {}
    for l in out.splitlines():
        m = re.match(r"(\S+)\s+_adb-tls-(connect|pairing)\._tcp\.?\s+([\d.]+:\d+)", l)
        if m:
            name = re.sub(r"^adb-|-\w+$", "", m.group(1))
            found.setdefault(name, {"name": name})[m.group(2)] = m.group(3)
    return {"services": list(found.values())}


def device_ip(serial):
    _, out = adb("-s", serial, "shell", "ip -f inet addr show wlan0")
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def switch_to_wifi(serial):
    ip = device_ip(serial)
    if not ip:
        return False, "Could not read the phone's Wi-Fi IP. Is Wi-Fi on?"
    code, out = adb("-s", serial, "tcpip", "5555")
    if code:
        return False, out
    time.sleep(2.5)
    code, out = adb("connect", ip + ":5555")
    ok = code == 0 and "connected" in out and "fail" not in out.lower()
    return ok, out + ("  You can unplug the cable now." if ok else "")


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "DroidFPS/1.0"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self.static("index.html", "text/html; charset=utf-8")
        if u.path == "/app.js":
            return self.static("app.js", "text/javascript; charset=utf-8")
        if u.path == "/style.css":
            return self.static("style.css", "text/css; charset=utf-8")
        if u.path == "/api/devices":
            return self.send_json(list_devices())
        if u.path == "/api/mdns":
            return self.send_json(mdns_services())
        if u.path == "/api/stream":
            qs = parse_qs(u.query)
            return self.stream(qs.get("serial", [""])[0], qs.get("mode", [""])[0] == "light")
        self.send_error(404)

    def static(self, name, ctype):
        with open(os.path.join(WEB, name), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self, serial, light=False):
        if not serial or not ADDR_RE.match(serial):
            return self.send_json({"error": "bad serial"}, 400)
        sampler = MANAGER.get(serial)
        sampler.light = light
        q = sampler.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if data is None:
                    break
                self.wfile.write(b"data: " + data + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            sampler.unsubscribe(q)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n < 10_000 else {}
        except ValueError:
            return self.send_json({"ok": False, "msg": "bad json"}, 400)
        path = urlparse(self.path).path
        addr = str(body.get("address", "")).strip()
        serial = str(body.get("serial", "")).strip()
        for v in (addr, serial):
            if v and not ADDR_RE.match(v):
                return self.send_json({"ok": False, "msg": "invalid address"}, 400)
        if path == "/api/connect":
            if ":" not in addr:
                addr += ":5555"
            code, out = adb("connect", addr)
            if "protocol fault" in out or "failed to authenticate" in out:
                out += ("\nThat port didn't accept a connection. Use the IP & port on the main Wireless debugging"
                        " screen (not the pairing pop-up), and pair first if this PC hasn't been paired.")
            return self.send_json({"ok": code == 0 and "connected" in out and "fail" not in out.lower(), "msg": out})
        if path == "/api/pair":
            pin = str(body.get("code", "")).strip()
            if not pin.isdigit():
                return self.send_json({"ok": False, "msg": "pairing code must be digits"}, 400)
            code, out = adb("pair", addr, pin)
            if "protocol fault" in out:
                out += ("\nThat isn't the pairing port. Tap 'Pair device with pairing code' and use the IP & port"
                        " shown in that pop-up (it changes each time).")
            return self.send_json({"ok": code == 0 and "Successfully" in out, "msg": out})
        if path == "/api/disconnect":
            code, out = adb("disconnect", addr or serial)
            return self.send_json({"ok": code == 0, "msg": out})
        if path == "/api/wifi":
            ok, out = switch_to_wifi(serial)
            return self.send_json({"ok": ok, "msg": out})
        if path == "/api/mode":
            smp = MANAGER.samplers.get(serial)
            if smp:
                smp.light = bool(body.get("light"))
            return self.send_json({"ok": True, "light": bool(body.get("light"))})
        if path == "/api/reset":
            s = MANAGER.samplers.get(serial)
            if s:
                s.reset()
            return self.send_json({"ok": True})
        self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description="Live Android FPS / thermal monitor over ADB")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    url = "http://%s:%d" % ("127.0.0.1" if a.host in ("0.0.0.0", "") else a.host, a.port)
    print("DroidFPS running at", url, "(Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
