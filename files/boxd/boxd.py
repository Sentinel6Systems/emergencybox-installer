#!/usr/bin/env python3
"""boxd: tiny local service behind the EmergencyBox landing page.

GET  /api/stats     live system stats (5-second averages), JSON
POST /api/shutdown  safe poweroff   (both need the X-Box-Action: 1 header the landing page sends)
POST /api/reboot    reboot
GET  /api/clients   devices on the hotspot (name, type, IP, MAC, signal, data)
GET  /api/parts     which parts of the box are installed, and how much content each has
GET  /api/doctor    the latest box-doctor result (self-check, every 15 minutes)

Python standard library only. Listens on 127.0.0.1; nginx proxies /api/ to it.
"""
import glob, json, os, re, shutil, subprocess, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", int(os.environ.get("BOXD_PORT", "8181"))
WINDOW = 5                      # seconds averaged
DISKS = [p for p in os.environ.get("BOXD_DISKS", "/").split(":") if p]
HOTSPOT_IF = os.environ.get("BOXD_HOTSPOT_IF", "wlan0")
CONTENT = os.environ.get("BOXD_CONTENT", "/srv/box/content")
MAPS = os.environ.get("BOXD_MAPS", "/opt/fieldmap-server/data")   # fieldmap-server keeps its own data
PARTS_DIR = "/etc/emergencybox/parts"          # each module drops a file named after its part
DOCTOR_FILE = "/run/emergencybox/doctor.json"  # written by box-doctor
TEMP_WARN, TEMP_HOT = 65.0, 80.0  # Pi 4 firmware starts throttling at 80 C

samples = deque(maxlen=WINDOW + 1)   # one per second
lock = threading.Lock()
slow = {"clients": None, "throttled": None, "t": 0}


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def cpu_times():
    f = read("/proc/stat").split("\n", 1)[0].split()[1:]
    v = [int(x) for x in f]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v), idle


def net_bytes():
    rx = tx = 0
    rows = read("/proc/net/dev").splitlines()[2:]
    ifs = {}
    for r in rows:
        name, data = r.split(":", 1)
        d = data.split()
        ifs[name.strip()] = (int(d[0]), int(d[8]))
    if HOTSPOT_IF in ifs:
        return ifs[HOTSPOT_IF]
    for name, (r, t) in ifs.items():
        if name != "lo" and not name.startswith(("veth", "docker")):
            rx += r; tx += t
    return rx, tx


def mem():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    total = m.get("MemTotal", 0)
    return total, total - m.get("MemAvailable", 0)


def temp_c():
    t = read("/sys/class/thermal/thermal_zone0/temp").strip()
    return int(t) / 1000 if t else None


def sample():
    total, idle = cpu_times()
    rx, tx = net_bytes()
    mt, mu = mem()
    return {"t": time.monotonic(), "cpu": (total, idle), "net": (rx, tx), "mem": mu, "memt": mt, "temp": temp_c()}


def ap_interfaces():
    try:
        out = subprocess.run(["iw", "dev"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ifs, cur = [], None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Interface "):
            cur = line.split()[1]
        elif line == "type AP" and cur:
            ifs.append(cur)
    return ifs


def count_clients():
    ifs = ap_interfaces()
    if ifs:
        n = 0
        for i in ifs:
            try:
                out = subprocess.run(["iw", "dev", i, "station", "dump"], capture_output=True, text=True, timeout=3).stdout
                n += out.count("Station ")
            except (OSError, subprocess.SubprocessError):
                pass
        return n
    # fallback: reachable neighbours on the hotspot bridge
    n = 0
    for line in read("/proc/net/arp").splitlines()[1:]:
        p = line.split()
        if len(p) >= 6 and p[5] == HOTSPOT_IF and p[2] != "0x0":
            n += 1
    return n


def throttled():
    """Pi firmware flags: under-voltage / throttling now, or since boot."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        v = int(out.strip().split("=")[1], 16)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None
    return {"undervolt_now": bool(v & 0x1), "throttled_now": bool(v & 0x4),
            "undervolt_since_boot": bool(v & 0x10000), "throttled_since_boot": bool(v & 0x40000)}


def sampler():
    while True:
        s = sample()
        with lock:
            samples.append(s)
        now = time.monotonic()
        if now - slow["t"] >= 5:
            slow.update(clients=count_clients(), throttled=throttled(), t=now)
        time.sleep(1)


def stats():
    with lock:
        ss = list(samples)
    if len(ss) < 2:
        return {"ready": False}
    a, b = ss[0], ss[-1]
    dt = max(b["t"] - a["t"], 0.001)
    dtot = b["cpu"][0] - a["cpu"][0]
    didle = b["cpu"][1] - a["cpu"][1]
    cpu = 100 * (1 - didle / dtot) if dtot > 0 else 0
    rx = (b["net"][0] - a["net"][0]) * 8 / dt / 1e6
    tx = (b["net"][1] - a["net"][1]) * 8 / dt / 1e6
    mem_used = sum(s["mem"] for s in ss[1:]) / (len(ss) - 1)
    temps = [s["temp"] for s in ss[1:] if s["temp"] is not None]
    t = sum(temps) / len(temps) if temps else None
    disks = []
    for p in DISKS:
        try:
            u = shutil.disk_usage(p)
            disks.append({"path": p, "used": u.used, "total": u.total})
        except OSError:
            pass
    up = float(read("/proc/uptime", "0").split()[0])
    load = os.getloadavg()
    return {
        "ready": True, "window_s": round(dt, 1),
        "cpu_pct": round(cpu, 1),
        "temp_c": round(t, 1) if t is not None else None,
        "temp_level": None if t is None else ("hot" if t >= TEMP_HOT else "warm" if t >= TEMP_WARN else "ok"),
        "mem_used": int(mem_used), "mem_total": b["memt"],
        "net_rx_mbps": round(rx, 2), "net_tx_mbps": round(tx, 2), "net_if": HOTSPOT_IF,
        "clients": slow["clients"],
        "disks": disks,
        "uptime_s": int(up),
        "load1": round(load[0], 2),
        "power": slow["throttled"],
    }


# ---------- connected devices ----------
LEASE_FILES = ["/var/lib/NetworkManager/dnsmasq-%s.leases" % HOTSPOT_IF, "/var/lib/misc/dnsmasq.leases", "/var/lib/dnsmasq/dnsmasq.leases", "/var/lib/dhcp/dhcpd.leases"]
OUI_FILES = ["/usr/share/ieee-data/oui.txt", "/usr/share/misc/oui.txt"]
_oui = None


def oui_vendor(mac):
    global _oui
    if _oui is None:
        _oui = {}
        for f in OUI_FILES:
            for line in read(f).splitlines():
                m = re.match(r"^([0-9A-F]{2})-([0-9A-F]{2})-([0-9A-F]{2})\s+\(hex\)\s+(.+)$", line.strip())
                if m:
                    _oui["".join(m.groups()[:3])] = m.group(4).strip()
            if _oui:
                break
    return _oui.get(mac.upper().replace(":", "")[:6])


def leases():
    """mac -> {ip, name} from dnsmasq or ISC dhcpd lease files."""
    out = {}
    for f in LEASE_FILES:
        txt = read(f)
        if not txt:
            continue
        if "lease " in txt and "{" in txt:          # ISC dhcpd format
            for blk in re.finditer(r"lease ([\d.]+) \{(.*?)\}", txt, re.S):
                ip, body = blk.group(1), blk.group(2)
                m = re.search(r"hardware ethernet ([0-9a-f:]+);", body)
                h = re.search(r'client-hostname "([^"]*)";', body)
                if m:
                    out[m.group(1).lower()] = {"ip": ip, "name": h.group(1) if h else None}
        else:                                        # dnsmasq: expiry mac ip name clientid
            for line in txt.splitlines():
                p = line.split()
                if len(p) >= 4:
                    out[p[1].lower()] = {"ip": p[2], "name": None if p[3] == "*" else p[3]}
    return out


def arp():
    out = {}
    for line in read("/proc/net/arp").splitlines()[1:]:
        p = line.split()
        if len(p) >= 6 and p[2] != "0x0":
            out[p[3].lower()] = {"ip": p[0], "dev": p[5]}
    return out


def guess_type(name, vendor):
    t = f"{name or ''} {vendor or ''}".lower()
    if re.search(r"iphone|android|pixel|galaxy|sm-|moto|oneplus|phone|xiaomi|redmi", t): return "phone"
    if re.search(r"ipad|tablet|tab-|kindle", t): return "tablet"
    if re.search(r"macbook|laptop|desktop-|thinkpad|notebook|pc|imac|mac", t): return "computer"
    if re.search(r"tv|roku|chromecast|firetv|shield", t): return "tv"
    if re.search(r"epson|canon|brother|hp|printer|lexmark", t): return "printer"
    return "unknown"


def stations():
    res = []
    for i in ap_interfaces():
        try:
            out = subprocess.run(["iw", "dev", i, "station", "dump"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        cur = None
        for line in out.splitlines():
            m = re.match(r"Station ([0-9a-f:]{17})", line)
            if m:
                cur = {"mac": m.group(1).lower(), "iface": i}; res.append(cur); continue
            if not cur or ":" not in line:
                continue
            k, v = [x.strip() for x in line.split(":", 1)]
            num = re.match(r"-?\d+(\.\d+)?", v)
            if k == "signal" and num: cur["signal_dbm"] = int(float(num.group()))
            elif k == "connected time" and num: cur["connected_s"] = int(num.group())
            elif k == "inactive time" and num: cur["idle_ms"] = int(num.group())
            elif k == "rx bytes" and num: cur["from_device_bytes"] = int(num.group())
            elif k == "tx bytes" and num: cur["to_device_bytes"] = int(num.group())
            elif k == "tx bitrate" and num: cur["link_mbps"] = float(num.group())
    return res


def clients(requester_ip=None):
    lz, ar = leases(), arp()
    ip_by_mac = {m: v["ip"] for m, v in ar.items()}
    out, seen = [], set()
    for st in stations():
        mac = st["mac"]; seen.add(mac)
        l = lz.get(mac, {})
        ip = l.get("ip") or ip_by_mac.get(mac)
        out.append({**st, "link": "Wi-Fi", "ip": ip, "name": l.get("name")})
    # wired or otherwise reachable devices on the hotspot bridge that are not Wi-Fi stations
    for mac, a in ar.items():
        if mac in seen or a["dev"] != HOTSPOT_IF:
            continue
        l = lz.get(mac, {})
        out.append({"mac": mac, "link": "Wired / other", "ip": a["ip"], "name": l.get("name")})
    for c in out:
        mac = c["mac"]
        c["random_mac"] = bool(int(mac[:2], 16) & 0x02)   # locally administered = private address
        c["vendor"] = None if c["random_mac"] else oui_vendor(mac)
        c["type"] = guess_type(c["name"], c["vendor"])
        c["you"] = bool(requester_ip and c.get("ip") == requester_ip)
    out.sort(key=lambda c: (not c["you"], (c["name"] or "~").lower()))
    return out


# ---------- parts and content (blank-slate boxes show what's empty) ----------
# part -> (folder under CONTENT, file endings that count as content; None = any file[, smallest size])
CONTENT_OF = {
    "library": ("zims", (".zim",)),
    "books": ("books", (".epub", ".pdf", ".mobi", ".azw3", ".txt", ".cbz")),
    # absolute: os.path.join keeps it. fieldmap-server's empty placeholders (a few KB) don't count.
    "maps": (MAPS, (".mbtiles", ".pmtiles"), 1 << 20),
    "movies": ("media", (".mp4", ".mkv", ".m4v", ".webm", ".avi", ".mov")),
    "games": ("games/roms", None),
}
_parts = {"t": 0, "v": None}


def _big_enough(path, min_bytes):
    try:
        return os.path.getsize(path) >= min_bytes
    except OSError:
        return False


def _count(sub, ends, min_bytes=0, limit=5000):
    n = 0
    for root, dirs, files in os.walk(os.path.join(CONTENT, sub)):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "lost+found"]
        for f in files:
            if not f.startswith(".") and (ends is None or f.lower().endswith(ends)) \
                    and (not min_bytes or _big_enough(os.path.join(root, f), min_bytes)):
                n += 1
                if n >= limit:
                    return n
    return n


def parts():
    now = time.time()
    if _parts["v"] is not None and now - _parts["t"] < 60:
        return _parts["v"]
    try:
        installed = set(os.listdir(PARTS_DIR))
    except OSError:
        installed = set()
    out = {}
    for name in sorted(installed | set(CONTENT_OF)):
        d = {"installed": name in installed}
        if name in CONTENT_OF:
            d["items"] = _count(*CONTENT_OF[name]) if d["installed"] else 0
        out[name] = d
    _parts.update(t=now, v=out)
    return out


def doctor():
    try:
        with open(DOCTOR_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"ok": None, "checks": [], "note": "box-doctor hasn't run yet"}


def later(cmd):
    # reply first, then act, so the browser gets its answer
    threading.Timer(1.5, lambda: subprocess.run(cmd)).start()


class H(BaseHTTPRequestHandler):
    server_version = "boxd"

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/stats":
            return self.send(200, stats())
        if p == "/api/parts":
            return self.send(200, {"parts": parts()})
        if p == "/api/doctor":
            return self.send(200, doctor())
        if p == "/api/clients":
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            return self.send(200, {"clients": clients(ip), "you_ip": ip})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        # Only the box's own page: it sends this header. A form or a script on another site
        # can't (a custom header needs CORS, which boxd never allows).
        if self.headers.get("X-Box-Action") != "1" or self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return self.send(403, {"error": "not from the box's page"})
        if p == "/api/shutdown":
            later(["systemctl", "poweroff"]); return self.send(200, {"ok": True, "action": "poweroff"})
        if p == "/api/reboot":
            later(["systemctl", "reboot"]); return self.send(200, {"ok": True, "action": "reboot"})
        self.send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        if self.command == "POST":
            super().log_message(fmt, *args)


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
