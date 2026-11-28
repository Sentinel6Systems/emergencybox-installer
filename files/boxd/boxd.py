#!/usr/bin/env python3
"""boxd: tiny local service behind the EmergencyBox landing page.

GET  /api/stats     live system stats (5-second averages), JSON
POST /api/shutdown  safe poweroff   (both need the X-Box-Action: 1 header the landing page sends)
POST /api/reboot    reboot
GET  /api/clients   devices on the hotspot (name, type, IP, MAC, signal, data)
GET  /api/parts     which parts of the box are installed, and how much content each has
GET  /api/doctor    the latest box-doctor result (self-check, every 15 minutes)
GET  /api/power     power source (wall or PiSugar battery), battery shutdown countdown, Pi power flags
GET  /api/network   connections (Ethernet, Wi-Fi, VPN) and their traffic: ?range=30s|5m|24h|1w

Python standard library only. Listens on 127.0.0.1; nginx proxies /api/ to it.
"""
import glob, json, os, re, shlex, shutil, signal, subprocess, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOST, PORT = "127.0.0.1", int(os.environ.get("BOXD_PORT", "8181"))
WINDOW = 5                      # seconds averaged
DISKS = [p for p in os.environ.get("BOXD_DISKS", "/").split(":") if p]
HOTSPOT_IF = os.environ.get("BOXD_HOTSPOT_IF", "wlan0")
CONTENT = os.environ.get("BOXD_CONTENT", "/srv/box/content")
MAPS = os.environ.get("BOXD_MAPS", "/opt/fieldmap-server/data")   # fieldmap-server keeps its own data
PARTS_DIR = "/etc/emergencybox/parts"          # each module drops a file named after its part
DOCTOR_FILE = "/run/emergencybox/doctor.json"  # written by box-doctor
STATE_DIR = os.environ.get("BOXD_STATE", "/var/lib/boxd")
NET_HISTORY = os.path.join(STATE_DIR, "net-history.json")
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


def net_counters():
    """interface -> (rx bytes, tx bytes)"""
    ifs = {}
    for r in read("/proc/net/dev").splitlines()[2:]:
        name, data = r.split(":", 1)
        d = data.split()
        ifs[name.strip()] = (int(d[0]), int(d[8]))
    return ifs


def net_bytes(ifs):
    rx = tx = 0
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
    ifs = net_counters()
    mt, mu = mem()
    return {"t": time.monotonic(), "cpu": (total, idle), "net": net_bytes(ifs), "ifs": ifs, "mem": mu, "memt": mt, "temp": temp_c()}


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


def uplink():
    """(interface, its kind label) of the default route: the way out to the internet. None = offline."""
    for line in run(["ip", "-4", "route", "show", "default"]).splitlines():
        m = re.search(r"\bdev (\S+)", line)
        if m:
            kind, label = iface_kind(m.group(1))
            return {"if": m.group(1), "label": label or m.group(1)}
    return None


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
    saved = time.monotonic()
    n = 0
    while True:
        n += 1
        if n % 2 == 0:
            power.tick()
        s = sample()
        with lock:
            prev = samples[-1] if samples else None
            samples.append(s)
        if prev:
            net_record(prev, s)
        now = time.monotonic()
        if now - slow["t"] >= 5:
            slow.update(clients=count_clients(), throttled=throttled(), uplink=uplink(), t=now)
        if now - saved >= 300:          # the 24-hour and week history survive a restart
            net_save(); saved = now
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
    # Wi-Fi and Ethernet each: down = toward the people using the box (what the hotspot sends,
    # what any other connection receives)
    links = {}
    for name, (r1, t1) in b["ifs"].items():
        if name.startswith(SKIP_IF) or name not in a["ifs"]:
            continue
        kind = "wifi" if os.path.isdir(f"/sys/class/net/{name}/wireless") else \
            "ethernet" if iface_kind(name)[0] == "ethernet" else None
        if not kind:
            continue
        r0, t0 = a["ifs"][name]
        down, up = (t1 - t0, r1 - r0) if name == HOTSPOT_IF else (r1 - r0, t1 - t0)
        l = links.setdefault(kind, {"down_mbps": 0.0, "up_mbps": 0.0})
        l["down_mbps"] += max(down, 0) * 8 / dt / 1e6
        l["up_mbps"] += max(up, 0) * 8 / dt / 1e6
    for l in links.values():
        l["down_mbps"], l["up_mbps"] = round(l["down_mbps"], 2), round(l["up_mbps"], 2)
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
        "net": links, "uplink": slow.get("uplink"),
        "clients": slow["clients"],
        "disks": disks,
        "uptime_s": int(up),
        "load1": round(load[0], 2),
        "power": power_status(),
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


# ---------- network: each connection and its traffic over time ----------
# range -> (seconds per point, points kept). Every second's traffic is added to all four.
RANGES = {"30s": (1, 30), "5m": (5, 60), "15m": (15, 60), "60m": (60, 60),
          "12h": (300, 144), "24h": (300, 288), "1w": (3600, 168)}
KEEP = ("5m", "15m", "60m", "12h", "24h", "1w")   # written to NET_HISTORY
SKIP_IF = ("lo", "docker", "veth", "br-", "virbr", "p2p-")
VPN_NAMES = (("tailscale", "Tailscale"), ("wt", "NetBird"), ("wg", "WireGuard"), ("zt", "ZeroTier"),
             ("tun", "VPN"), ("tap", "VPN"), ("ppp", "VPN"))
hist = {r: {} for r in RANGES}          # range -> interface -> {point number: [rx bytes, tx bytes, seconds]}
ORDER = {"hotspot": 0, "ethernet": 1, "wifi": 2, "vpn": 3}


def net_record(a, b):
    """Add the traffic between two samples to every range. Point numbers count from 1970,
    so the saved history lines up again after a restart."""
    secs = min(b["t"] - a["t"], 5.0)    # a stalled sampler doesn't count as hours of silence
    now = time.time()
    with lock:
        for name, (rx, tx) in b["ifs"].items():
            if name.startswith(SKIP_IF) or name not in a["ifs"]:
                continue
            drx, dtx = rx - a["ifs"][name][0], tx - a["ifs"][name][1]
            if drx < 0 or dtx < 0:          # the interface was re-created: its counters restarted
                continue
            for r, (step, n) in RANGES.items():
                pts = hist[r].setdefault(name, {})
                cur = int(now // step)
                p = pts.get(cur)
                if p is None:               # a new point: drop the ones that fell out of the range
                    for k in [k for k in pts if k <= cur - n or k > cur]:
                        del pts[k]
                    p = pts[cur] = [0, 0, 0.0]
                p[0] += drx; p[1] += dtx; p[2] += secs


def net_load():
    try:
        with open(NET_HISTORY) as f:
            d = json.load(f)
        for r in KEEP:
            for name, pts in d.get(r, {}).items():
                hist[r][name] = {int(k): v for k, v in pts.items()}
        for name, pts in hist["24h"].items():     # same 5-minute points: 12 h fills its gaps from the 24 h history
            mine = hist["12h"].setdefault(name, {})
            for k, v in pts.items():
                mine.setdefault(k, list(v))
    except (OSError, ValueError, AttributeError, TypeError):
        pass


def net_save():
    with lock:
        d = {r: {name: {str(k): v for k, v in pts.items()} for name, pts in hist[r].items()} for r in KEEP}
    try:
        os.makedirs(os.path.dirname(NET_HISTORY), exist_ok=True)
        with open(NET_HISTORY + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(NET_HISTORY + ".tmp", NET_HISTORY)
    except OSError:
        pass


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def iface_kind(name):
    """(kind, label) or (None, None) for interfaces the page doesn't show (bridges, dummies)."""
    s = "/sys/class/net/" + name
    if os.path.isdir(s + "/wireless") or os.path.exists(s + "/phy80211"):
        return "wifi", "Wi-Fi"
    for pre, label in VPN_NAMES:
        if name.startswith(pre):
            return "vpn", label
    if read(s + "/type").strip() == "65534":    # no hardware address: a tunnel
        return "vpn", "VPN"
    if os.path.exists(s + "/device"):
        return "ethernet", "Ethernet"
    return None, None


def iw_info(name, cmd):
    out = {}
    for line in run(["iw", "dev", name] + cmd).splitlines():
        line = line.strip()
        for key, pat in (("type", r"^type (\S+)"), ("ssid", r"^ssid (.+)"), ("ssid", r"^SSID: (.+)"),
                         ("channel", r"^channel (\d+)"), ("signal_dbm", r"^signal: (-?\d+)"),
                         ("link_mbps", r"^tx bitrate: ([\d.]+)")):
            m = re.match(pat, line)
            if m:
                out[key] = m.group(1)
    return out


def series(name, rng, ap):
    """Points for one interface, oldest first: [down, up] bits per second, or None where the box was off.
    down = toward the people using the box: what the hotspot sends, what every other connection receives."""
    step, n = RANGES[rng]
    cur = int(time.time() // step)
    with lock:
        pts = dict(hist[rng].get(name, {}))
    d, u, tot_d, tot_u, secs = [], [], 0, 0, 0.0
    for k in range(cur - n + 1, cur + 1):
        p = pts.get(k)
        if not p or p[2] <= 0:
            d.append(None); u.append(None); continue
        down, up = (p[1], p[0]) if ap else (p[0], p[1])
        d.append(round(down * 8 / p[2])); u.append(round(up * 8 / p[2]))
        tot_d += down; tot_u += up; secs += p[2]
    peak = lambda xs: max((x for x in xs if x is not None), default=None)
    return {"step_s": step, "down": d, "up": u, "total_down": tot_d, "total_up": tot_u, "recorded_s": round(secs),
            "avg_down": round(tot_d * 8 / secs) if secs else None, "avg_up": round(tot_u * 8 / secs) if secs else None,
            "peak_down": peak(d), "peak_up": peak(u)}


def network(rng):
    if rng not in RANGES:
        rng = "30s"
    addrs = {}
    try:
        for i in json.loads(run(["ip", "-j", "addr", "show"]) or "[]"):
            addrs[i.get("ifname")] = (i.get("operstate", "").lower(),
                                      [a["local"] for a in i.get("addr_info", []) if a.get("family") == "inet"])
    except ValueError:
        pass
    try:
        routes = json.loads(run(["ip", "-j", "route", "show", "default"]) or "[]")
    except ValueError:
        routes = []
    internet = {r.get("dev") for r in routes}
    with lock:
        known = set(hist["1w"]) | set(hist["30s"])
    present = {n for n in os.listdir("/sys/class/net") if not n.startswith(SKIP_IF)}
    out = []
    for name in sorted(present | known):
        gone = name not in present
        if gone:                            # a VPN or USB adapter that is gone now: show its history
            kind, label = next((("vpn", l) for p, l in VPN_NAMES if name.startswith(p)), ("ethernet", "Ethernet"))
            if name.startswith("wl"):
                kind, label = "wifi", "Wi-Fi"
        else:
            kind, label = iface_kind(name)
            if not kind:
                continue
        state, ips = addrs.get(name, ("down", []))
        c = {"name": name, "kind": kind, "label": label, "addresses": ips, "internet": name in internet}
        up = not gone and state in ("up", "unknown")
        if kind == "wifi" and not gone:
            info = iw_info(name, ["info"])
            if info.get("type") == "AP":
                c.update(kind="hotspot", label="Wi-Fi hotspot", ssid=info.get("ssid"), devices=slow["clients"],
                         channel=int(info["channel"]) if "channel" in info else None)
            else:
                link = iw_info(name, ["link"])
                up = up and "ssid" in link
                c.update(ssid=link.get("ssid"), signal_dbm=int(link["signal_dbm"]) if "signal_dbm" in link else None,
                         link_mbps=float(link["link_mbps"]) if "link_mbps" in link else None)
        elif kind == "ethernet" and not gone:
            up = up and read("/sys/class/net/%s/carrier" % name).strip() == "1"
            speed = read("/sys/class/net/%s/speed" % name).strip()
            c["link_mbps"] = int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None
        c["connected"] = up
        c["gone"] = gone
        c.update(series(name, rng, c["kind"] == "hotspot"))
        now = series(name, "30s", c["kind"] == "hotspot")      # the last 5 seconds, like the card
        last = [(x, y) for x, y in zip(now["down"][-6:-1], now["up"][-6:-1]) if x is not None]
        c["now_down"] = round(sum(x for x, _ in last) / len(last)) if last else None
        c["now_up"] = round(sum(y for _, y in last) / len(last)) if last else None
        if gone and not c["total_down"] and not c["total_up"] and c["recorded_s"] == 0:
            continue                        # nothing to show for this range
        out.append(c)
    out.sort(key=lambda c: (c["gone"], ORDER.get(c["kind"], 9), c["name"]))
    return {"range": rng, "connections": out}


# ---------- power source: a PiSugar S Plus tells wall power from battery on GPIO 3 ----------
# pinctrl shows "3: ip pu | lo" on wall power and "| hi" on battery. The S Plus has no fuel gauge,
# so there is no battery percentage. I2C must stay off: it would take the pin.
# Settings in POWER_CONF, read every check (a change needs no reinstall):
#   PISUGAR=yes|no            no = don't watch the pin (without a PiSugar it reads hi, which looks like battery)
#   BATTERY_SHUTDOWN_MIN=90   clean shutdown after this long on battery; 0 = never
PINCTRL = shlex.split(os.environ.get("BOXD_PINCTRL", "pinctrl get 3"))
POWEROFF = shlex.split(os.environ.get("BOXD_POWEROFF", "systemctl poweroff"))
POWER_CONF = os.environ.get("BOXD_POWER_CONF", "/etc/emergencybox/power.conf")
POWER_STATE = os.path.join(STATE_DIR, "power-source.json")
SHUTDOWN_MIN_DEFAULT = 90           # a full 5000 mAh S Plus runs a busy Pi 4 + SSD about 2-3 h
DEBOUNCE = 3                     # identical reads (2 s apart) before a change counts


def pin_source(out):
    """pinctrl output -> "external", "battery" or None (missing, unreadable, or not an input)."""
    m = re.match(r"\s*3:\s*(\w+)[^|\n]*\|\s*(lo|hi)\b", out or "")
    if not m or m.group(1) != "ip":
        return None
    return "external" if m.group(2) == "lo" else "battery"


def power_conf(text):
    """(watch the pin?, minutes on battery before shutdown). Bad values fall back to the defaults."""
    conf = {}
    for line in text.splitlines():
        k, _, v = line.partition("=")
        conf[k.strip()] = v.split("#")[0].strip().strip('"\'')
    m = conf.get("BATTERY_SHUTDOWN_MIN", "")
    minutes = int(m) if m.isdigit() and int(m) <= 1440 else SHUTDOWN_MIN_DEFAULT
    return conf.get("PISUGAR") == "yes", minutes


class PowerWatch:
    """Debounced power source and the battery shutdown countdown. Clock, commands and files can be
    swapped for tests."""

    def __init__(self, run_cmd=None, now=time.time, poweroff=None, conf=POWER_CONF, state=POWER_STATE, boot_time=None):
        self.run = run_cmd or run
        self.now, self.conf, self.state = now, conf, state
        self.poweroff = poweroff or (lambda: later(POWEROFF))
        self.source, self.since, self.fired = "unknown", None, False
        self.cand, self.cand_t, self.count = None, None, 0
        self.minutes = SHUTDOWN_MIN_DEFAULT
        self.resume = None              # battery start time from before a boxd restart (not a reboot)
        try:
            with open(state) as f:
                d = json.load(f)
            boot = boot_time if boot_time is not None else now() - float(read("/proc/uptime", "0").split()[0])
            if d.get("source") == "battery" and float(d["since"]) > boot:
                self.resume = float(d["since"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def log(self, msg):
        print("power: " + msg, flush=True)      # the journal keeps it across the reboot

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.state), exist_ok=True)
            with open(self.state + ".tmp", "w") as f:
                json.dump({"source": self.source, "since": self.since}, f)
            os.replace(self.state + ".tmp", self.state)
        except OSError:
            pass

    def tick(self):
        watch, self.minutes = power_conf(read(self.conf))
        now = self.now()
        got = (pin_source(self.run(PINCTRL)) if watch else None) or "unknown"
        if got != self.cand:
            self.cand, self.cand_t, self.count = got, now, 0
        self.count += 1
        if self.count >= DEBOUNCE and got != self.source:
            old, was = self.source, self.since
            self.source, self.since, self.fired = got, self.cand_t, False
            if got == "battery" and self.resume:
                self.since = self.resume
            self.resume = None
            if got == "battery":
                self.log("on battery (GPIO 3 high)" + (f"; clean shutdown after {self.minutes} min unless wall power returns"
                                                       if self.minutes else "; battery shutdown is off"))
            elif old == "battery":
                self.log(f"wall power back after {round((now - was) / 60, 1)} min on battery; shutdown countdown cancelled")
            elif got == "unknown" and watch:
                self.log("can't read GPIO 3 (pinctrl missing, or the pin isn't an input): source unknown")
            self.save()
        if self.source == "battery" and self.minutes and not self.fired and now - self.since >= self.minutes * 60:
            self.fired = True
            self.log(f"on battery for {round((now - self.since) / 60, 1)} min (limit {self.minutes} min): shutting down cleanly")
            self.poweroff()

    def status(self):
        now = self.now()
        bat = self.source == "battery"
        left = max(0, round(self.since + self.minutes * 60 - now)) if bat and self.minutes else None
        return {"source": self.source, "on_battery_seconds": round(now - self.since) if bat else None,
                "shutdown_in_seconds": left, "shutdown_after_min": self.minutes or None}


power = PowerWatch()


def power_status():
    """Pi firmware flags (when vcgencmd works) plus the power source."""
    return {**(slow["throttled"] or {}), **power.status()}


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
        if p == "/api/power":
            return self.send(200, power_status())
        if p == "/api/network":
            return self.send(200, network(parse_qs(urlsplit(self.path).query).get("range", ["30s"])[0]))
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


def stop(*_):
    net_save()
    os._exit(0)


if __name__ == "__main__":
    net_load()
    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=sampler, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
