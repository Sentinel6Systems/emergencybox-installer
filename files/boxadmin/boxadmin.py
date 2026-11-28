#!/usr/bin/env python3
"""EmergencyBox admin: the owner's settings page behind the admin password (127.0.0.1:8185, behind nginx).

POST /api/admin/login      {password}                       sign in (cookie, 8 hours)
POST /api/admin/logout
GET  /api/admin/state      signed in?, settings, parts, content, USB drives, running job
POST /api/admin/wifi       {ssid, password}                 change the hotspot (runs the installer's network part)
POST /api/admin/passwords  {current, admin?, user?}         change the admin / shared user password
POST /api/admin/upload-check {kind, sub, name, size}       may this file go in, and under which name?
POST /api/admin/upload?kind=..&sub=..&name=..   raw body    add a file to the content (streamed to disk)
    Movies: sub is "<library>/<Title>", and the files are named after the title (see store_name).
POST /api/admin/delete     {kind, sub, name}                remove a content file
POST /api/admin/backup     {drive}                          My files + settings + map pairing to a USB drive
POST /api/admin/restore    {drive, file, settings}          My files (and optionally Wi-Fi/user password, map pairing) back
GET  /api/admin/job        the last background job (installer runs, backups)

Runs as root: it rewrites box.conf, runs the installer's parts, and writes to the content folders.
It only listens on 127.0.0.1. Every request but login needs the session cookie; every POST also
needs the X-Box-Admin header (a cross-site form can't send it) and a same-site cookie.
Settings live in box.conf, the installer's own file: the installer stays the one source of truth.
"""
import hashlib, hmac, http.server, io, json, os, re, secrets, shutil, socketserver, subprocess, sys
import ssl, tarfile, tempfile, threading, time, urllib.parse, urllib.request, urllib.error

E = os.environ.get
INSTALLER = E("BOXADMIN_INSTALLER", "/home/admin/emergencybox")
PASSFILE = E("BOXADMIN_PASSFILE", "/etc/emergencybox/admin.pass")
CONTENT_RW = E("BOXADMIN_CONTENT_RW", "/srv/box/content")       # where content is written
MYFILES = E("BOXADMIN_MYFILES", "/srv/box/myfiles")
MAPS = E("BOXADMIN_MAPS", "/opt/fieldmap-server/data")             # fieldmap-server's own data folder
FIELDMAP = os.path.dirname(MAPS)
RESERVE = int(E("BOXADMIN_RESERVE_GB", "5") or 0) << 30            # uploads never eat into this
PARTS_DIR = "/etc/emergencybox/parts"
JF_LIBS = E("BOXADMIN_JELLYFIN_LIBRARIES", "movies:Movies shows:Shows")
JOBDIR = "/run/emergencybox/admin-job"
LOG = "/var/log/emergency-box-admin.log"
PORT = int(E("BOXADMIN_PORT", "8185"))
CHUNK = 1 << 20
SESSION_HOURS = 8

MAP_LAYERS = ["basemap", "contours", "hillshade", "hillshade-detail", "elevation", "extras", "usdata"]
VIDEOS = (".mp4", ".mkv", ".m4v", ".webm", ".avi", ".mov")
SUBTITLES = (".srt", ".vtt", ".ass")
POSTERS = (".jpg", ".jpeg", ".png")
# kind -> (folder under the content root, allowed endings, what the owner sees)
KINDS = {
    "library": ("zims", (".zim",), "Library (ZIM files)"),
    "maps": (MAPS, (".mbtiles",), "Map layers"),          # absolute: os.path.join keeps it
    "games": ("games/roms", None, "Game ROMs"),
    "movies": ("media", VIDEOS + SUBTITLES + POSTERS, "Movies & shows"),
}
# Jellyfin's library type by folder name, as files/jellyfin/setup.py makes them (anything else: movies)
SHOW_LIBS, OTHER_LIBS = ("shows", "tv"), ("youtube", "music")
# Endings of a subtitle's name that Jellyfin reads as its language or kind (Movie.en.forced.srt)
SUB_TAGS = set("""en eng es spa fr fre fra de ger deu it ita pt por nl dut nld sv swe no nor nb nob da dan
    fi fin pl pol ru rus uk ukr cs cze ces hu hun ro rum ron el gre ell tr tur ar ara he heb hi hin zh chi zho
    ja jpn ko kor vi vie th tha id ind forced sdh cc default""".split())
NAME_OK = re.compile(r"^[^/\\\x00-\x1f]{1,200}$")
SUB_OK = re.compile(r"^[\w ,.!()&'-]{1,80}$")
PW_BAD = set("'\"\\`$<>&")
SSID_OK = re.compile(r"^[A-Za-z0-9 ._-]{1,32}$")
USB_RE = re.compile(r"^usb[0-9]+$")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg); self.code = code; self.msg = msg


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%F %T ") + msg + "\n")
    except OSError:
        pass


# ---------- box.conf (the installer's settings) -------------------------------------------------
def conf_path():
    p = os.path.join(INSTALLER, "box.conf")
    if not os.path.exists(p):
        shutil.copy(os.path.join(INSTALLER, "box.conf.default"), p)
    return p


CONF_LINE = re.compile(r'^([A-Z_]+)=(?:"([^"]*)"|([^\s#"]*))\s*(?:#.*)?$')


def conf_parse(text):
    """KEY="value" or KEY=value lines, as bash reads them here (a quoted value may hold #)."""
    vals = {}
    for line in text.splitlines():
        m = CONF_LINE.match(line)
        if m:
            vals[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return vals


def conf_get():
    vals = {}
    for p in (os.path.join(INSTALLER, "box.conf.default"), conf_path()):
        with open(p) as f:
            vals.update(conf_parse(f.read()))
    return vals


def conf_set(changes):
    p = conf_path()
    with open(p) as f:
        lines = f.read().split("\n")
    for k, v in changes.items():
        new = f'{k}="{v}"'
        for i, line in enumerate(lines):
            if re.match(rf"^{k}=", line):
                lines[i] = new
                break
        else:
            lines.append(new)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines))
    st = os.stat(p)
    os.chown(tmp, st.st_uid, st.st_gid); os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, p)


def check_pw(pw, what, minlen=4):
    if not isinstance(pw, str) or not (minlen <= len(pw) <= 63):
        raise ApiError(400, f"The {what} must be {minlen} to 63 characters.")
    if any(c.isspace() or c in PW_BAD for c in pw) or not pw.isascii():
        raise ApiError(400, f"The {what} can't have spaces or any of ' \" \\ ` $ < > &.")


# ---------- password and sessions --------------------------------------------------------------
def hash_pw(pw, salt=None, n=200_000):
    salt = salt or secrets.token_hex(16)
    return f"pbkdf2_sha256${n}${salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), n).hex()}"


def pw_ok(pw):
    try:
        _, n, salt, want = open(PASSFILE).read().strip().split("$")
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(hash_pw(pw, salt, int(n)), f"pbkdf2_sha256${n}${salt}${want}")


SESSIONS = {}
FAILS = {}            # ip -> [count, until]
LOCK = threading.Lock()


def new_session():
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = time.time() + SESSION_HOURS * 3600
    return tok


# ---------- background jobs (installer runs survive this service and the Wi-Fi going down) -----
JOB = {"name": None, "started": 0, "thread": None}


def job_state():
    try:
        name = open(os.path.join(JOBDIR, "name")).read().strip()
    except OSError:
        return None
    rc_p = os.path.join(JOBDIR, "rc")
    rc = open(rc_p).read().strip() if os.path.exists(rc_p) else None
    try:
        with open(os.path.join(JOBDIR, "log"), errors="replace") as f:
            tail = f.read()[-4000:]
    except OSError:
        tail = ""
    return {"name": name, "running": rc is None, "ok": rc == "0", "log": tail}


def job_busy():
    s = job_state()
    return s is not None and s["running"]


def start_job(name, argv=None, func=None):
    """argv: an installer run, detached (systemd-run) so it outlives us. func: a Python job (backups)."""
    if job_busy():
        raise ApiError(409, "Something else is still running. Wait for it to finish.")
    shutil.rmtree(JOBDIR, ignore_errors=True)
    os.makedirs(JOBDIR, mode=0o700)
    open(os.path.join(JOBDIR, "name"), "w").write(name)
    logf = os.path.join(JOBDIR, "log")
    log(f"job: {name}")
    if argv:
        q = " ".join("'" + a.replace("'", "'\\''") + "'" for a in argv)
        sh = f"cd '{INSTALLER}' && {q} > '{logf}' 2>&1; echo $? > '{JOBDIR}/rc'"
        started = False
        if shutil.which("systemd-run"):            # outside our cgroup: survives a restart of this service
            started = subprocess.run(["systemd-run", "--quiet", "--collect", f"--unit=boxadmin-job-{int(time.time())}",
                                      "/bin/sh", "-c", sh], capture_output=True).returncode == 0
        if not started:
            subprocess.Popen(["/bin/sh", "-c", sh], start_new_session=True)
    else:
        def run():
            with open(logf, "a") as f:
                try:
                    func(lambda m: (f.write(m + "\n"), f.flush()))
                    rc = 0
                except Exception as e:          # the owner sees the message in the job's log
                    f.write(f"Error: {e}\n"); rc = 1
            open(os.path.join(JOBDIR, "rc"), "w").write(str(rc))
        threading.Thread(target=run, daemon=True).start()


def installer(*parts):
    return [os.path.join(INSTALLER, "install.sh"), *parts]


# ---------- parts, content, drives -------------------------------------------------------------
def parts():
    try:
        return sorted(os.listdir(PARTS_DIR))
    except OSError:
        return []


def free(path):
    try:
        u = shutil.disk_usage(path)
        return {"free": u.free, "total": u.total}
    except OSError:
        return None


def jf_libs():
    out = []
    for e in JF_LIBS.split():
        folder, _, name = e.partition(":")
        out.append({"id": folder, "name": name or folder.capitalize(), "type": lib_type(folder)})
    return out


def lib_type(folder):
    return "shows" if folder in SHOW_LIBS else "other" if folder in OTHER_LIBS else "movies"


def kind_dir(kind, sub=""):
    if kind not in KINDS:
        raise ApiError(400, "Unknown kind of content.")
    base = os.path.join(CONTENT_RW, KINDS[kind][0])
    if kind == "games":
        if not sub or sub not in game_systems():
            raise ApiError(400, "Pick a game system.")
        return os.path.join(base, sub)
    if kind == "movies":
        lib, _, folder = sub.partition("/")
        if lib not in [l["id"] for l in jf_libs()]:
            raise ApiError(400, "Pick a library.")
        d = os.path.join(base, lib)
        if folder:                      # a show's or movie's own folder: one level, plain name
            if not SUB_OK.match(folder) or folder[0] in ". " or folder[-1] in ". ":
                raise ApiError(400, "Titles: letters, numbers, spaces and , . ! _ ( ) & ' - only.")
            d = os.path.join(d, folder)
        return d
    return base


def game_systems():
    d = os.path.join(CONTENT_RW, "games/roms")
    try:
        return sorted(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n)) and not n.startswith("."))
    except OSError:
        return []


def list_files(d, depth=2, ends=None):
    """Files up to depth folder levels (1 = only d itself) with these endings (None = any)."""
    out = []
    if not os.path.isdir(d):
        return out
    for root, dirs, files in os.walk(d):
        rel = os.path.relpath(root, d)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "lost+found" and level + 1 < depth]
        for f in sorted(files):
            if f.startswith(".") or (ends and not f.lower().endswith(ends)):
                continue
            p = os.path.join(root, f)
            try:
                out.append({"name": f if rel == "." else os.path.join(rel, f), "size": os.path.getsize(p)})
            except OSError:
                pass
    return sorted(out, key=lambda x: x["name"].lower())


def content():
    out = {}
    for kind, (sub, ends, label) in KINDS.items():
        d = os.path.join(CONTENT_RW, sub)
        if kind == "games":
            groups = [{"id": s, "name": s, "files": list_files(os.path.join(d, s), 1, ends)} for s in game_systems()]
        elif kind == "movies":
            groups = [{**l, "files": list_files(os.path.join(d, l["id"]), 3, ends)} for l in jf_libs()]
        elif kind == "maps":        # only the map layers: the map server keeps its own working files beside them
            groups = [{"id": "", "name": label, "files": [f for f in list_files(d, 1, ends) if f["name"][:-len(".mbtiles")] in MAP_LAYERS]}]
        else:
            groups = [{"id": "", "name": label, "files": list_files(d, 1, ends)}]
        out[kind] = {"label": label, "groups": groups}
    out["space"] = free(CONTENT_RW)
    return out


def drives():
    out = []
    for n in sorted(os.listdir("/media")) if os.path.isdir("/media") else []:
        p = os.path.join("/media", n)
        if USB_RE.match(n) and mounted(p):
            bdir = os.path.join(p, "EmergencyBox-backups")
            backups = sorted((f for f in os.listdir(bdir) if f.endswith(".tar.gz")), reverse=True) if os.path.isdir(bdir) else []
            out.append({"id": n, **(free(p) or {}), "fs": fstype(p), "backups": backups})
    return out


def mounted(p):
    try:
        with open("/proc/mounts") as f:
            return any(line.split()[1] == p for line in f)
    except OSError:
        return False


def fstype(p):
    try:
        with open("/proc/mounts") as f:
            for line in f:
                a = line.split()
                if a[1] == p:
                    return a[2]
    except OSError:
        pass
    return ""


def drive_path(d):
    if not isinstance(d, str) or not USB_RE.match(d) or not mounted(f"/media/{d}"):
        raise ApiError(404, "That USB drive isn't plugged in.")
    return f"/media/{d}"


def upload_target(kind, sub, name, size):
    """The folder an upload goes to, after every check that can be made before the bytes arrive."""
    d = kind_dir(kind, sub)
    lib, _, folder = sub.partition("/")
    if kind == "movies" and not folder and lib_type(lib) != "other":     # every movie and show in its own folder
        raise ApiError(400, "Type the show's name." if lib_type(lib) == "shows" else "Type the movie's title.")
    if not isinstance(name, str) or not NAME_OK.match(name) or name.startswith(".") or name in (".", ".."):
        raise ApiError(400, "That file name can't be used.")
    ends = KINDS[kind][1]
    if ends and not name.lower().endswith(ends):
        raise ApiError(400, f"{KINDS[kind][2]} takes {', '.join(ends)} files.")
    if kind == "maps" and name[:-len(".mbtiles")] not in MAP_LAYERS:
        raise ApiError(400, "Map files must be named like the map server's layers: " + ", ".join(f"{m}.mbtiles" for m in MAP_LAYERS))
    space = free(CONTENT_RW) or {"free": 0}
    if size + RESERVE + (200 << 20) > space["free"]:
        room = max(0, space["free"] - RESERVE - (200 << 20))
        raise ApiError(507, f"Not enough room for {name}: it is {size >> 20} MB and there is room for {room >> 20} MB "
                            f"({RESERVE >> 30} GB is kept free for the system).")
    return d, store_name(kind, sub, name)


def store_name(kind, sub, name):
    """The name a file is kept under. In a movies library the parts of a movie are named after its
    folder, the names Jellyfin pairs up by itself: <Title>/<Title>.mp4, <Title>/<Title>.en.srt,
    <Title>/poster.jpg. A show's episodes keep their names (Jellyfin reads S01E02); its poster is poster.jpg."""
    if kind != "movies":
        return name
    lib, _, folder = sub.partition("/")
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext in POSTERS:
        if lib_type(lib) == "other":
            raise ApiError(400, "Posters go in a movie's or a show's folder.")
        return "poster" + (".jpg" if ext == ".jpeg" else ext)
    if lib_type(lib) != "movies":
        return name
    tags = []
    if ext in SUBTITLES:
        for t in reversed(stem.split(".")[1:]):
            if t.lower() not in SUB_TAGS:
                break
            tags.insert(0, "." + t.lower())
    return folder + "".join(tags) + ext


# ---------- after content changes ---------------------------------------------------------------
def has_video(d):
    for root, dirs, files in os.walk(d):
        if any(f.lower().endswith(VIDEOS) and not f.startswith(".") for f in files):
            return True
    return False


def jf_scan():
    """Jellyfin only watches a library folder once a scan has found something in it, so the first
    movie in an empty library needs a scan; after that, new files show up by themselves."""
    try:
        tok = jf("POST", "/Users/AuthenticateByName", {"Username": "admin", "Pw": conf_get().get("ADMIN_PASSWORD", "")})["AccessToken"]
        jf("POST", "/Library/Refresh", token=tok)
        log("jellyfin: library scan started (first movie in a library)")
    except Exception as e:
        log(f"jellyfin: library scan failed: {e!r}")


def after_change(kind):
    if kind == "library" and shutil.which("box-kiwix-library"):
        subprocess.run(["box-kiwix-library"], capture_output=True, timeout=300)
        return "Library list rebuilt."
    if kind == "maps":
        start_job("Map server picking up the new map files", installer("maps"))
        return "The map server is picking up the change (a few minutes)."
    if kind == "movies":
        return "Jellyfin adds it by itself within a minute or two."
    return ""


# ---------- Jellyfin and Calibre-Web passwords --------------------------------------------------
JF = "http://127.0.0.1:8096/jellyfin"
JF_AUTH = 'MediaBrowser Client="EmergencyBox", Device="box", DeviceId="boxadmin", Version="1"'


def jf(method, path, body=None, token=None):
    h = {"Content-Type": "application/json", "Authorization": JF_AUTH + (f', Token="{token}"' if token else "")}
    req = urllib.request.Request(JF + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def jf_set_passwords(old_admin, new_admin, user_name, new_user):
    try:
        auth = jf("POST", "/Users/AuthenticateByName", {"Username": "admin", "Pw": old_admin})
    except urllib.error.HTTPError as e:
        raise ApiError(409, f"Jellyfin didn't accept the current admin password ({e.code}); nothing was changed.")
    tok = auth["AccessToken"]
    users = {u["Name"]: u["Id"] for u in jf("GET", "/Users", token=tok)}

    def setpw(uid, body):
        for path in (f"/Users/Password?userId={uid}", f"/Users/{uid}/Password"):   # 10.9+ and older servers
            try:
                jf("POST", path, body, tok)
                return
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise ApiError(502, f"Jellyfin refused the password change ({e.code}).")
        raise ApiError(502, "This Jellyfin has no password-change address the box knows.")

    if new_user and user_name in users:
        setpw(users[user_name], {"NewPw": new_user})
    if new_admin and "admin" in users:
        setpw(users["admin"], {"CurrentPw": old_admin, "NewPw": new_admin})


def calibre_set_admin(pw):
    conf = E("BOXADMIN_CALIBRE_CONF", "/var/lib/calibre-web")
    cps = "/opt/calibre-web/venv/bin/cps"
    if not os.path.exists(cps):
        return
    subprocess.run(["systemctl", "stop", "calibre-web"], check=False)
    try:
        r = subprocess.run(["runuser", "-u", "calibreweb", "--", "env", f"HOME={conf}", f"CALIBRE_DBPATH={conf}",
                            cps, "-p", os.path.join(conf, "app.db"), "-s", f"admin:{pw}"], capture_output=True, timeout=120)
        if r.returncode:
            raise ApiError(502, "Calibre-Web didn't take the new admin password.")
    finally:
        subprocess.run(["systemctl", "start", "calibre-web"], check=False)


# ---------- backup and restore -----------------------------------------------------------------
# fieldmap-server's own state: its remote access (the certificate authority the paired phones trust,
# their list, NetBird) and its sharing settings, plus the .env lines the owner chose (the gateway's
# port, PINs). Without them a rebuilt box makes a new authority and every phone has to pair again.
# They hold private keys, so the backup drive must be kept private. Its run/ folders hold sockets.
MAP_DIRS = ("remote", "share")
MAP_ENV_KEYS = ("REMOTE_PORT", "REMOTE_PIN", "REMOTE_ADDRESS", "REMOTE_DIRECT", "REMOTE_RELAY", "ADMIN_PIN")
MAP_ENV_LINE = re.compile(r"^(" + "|".join(MAP_ENV_KEYS) + r")=(.*)$")


def map_env():
    try:
        with open(os.path.join(FIELDMAP, ".env")) as f:
            return [l.rstrip("\n") for l in f if MAP_ENV_LINE.match(l.rstrip("\n"))]
    except OSError:
        return []


def map_filter(ti):
    bits = ti.name.split("/")
    if len(bits) > 2 and bits[2] == "run":
        return None
    return ti if ti.isfile() or ti.isdir() else None


def add_map_state(t):
    found = False
    for sub in MAP_DIRS:
        p = os.path.join(MAPS, sub)
        if os.path.isdir(p):
            t.add(p, arcname=f"mapserver/{sub}", filter=map_filter)
            found = True
    env = map_env()
    if env:
        data = ("\n".join(env) + "\n").encode()
        ti = tarfile.TarInfo("mapserver/env")
        ti.size, ti.mode, ti.mtime = len(data), 0o600, int(time.time())
        t.addfile(ti, io.BytesIO(data))
        found = True
    return found


def restore_map(files, env, say):
    """Puts fieldmap-server's remote access and sharing state back and has it pick that up."""
    if not os.path.isdir(MAPS):
        say("Map server: not installed on this box, so its pairing was not restored.")
        return
    st = os.stat(FIELDMAP)                          # its owner runs its services and owns its data
    units = os.path.exists("/etc/systemd/system/fieldmap-remote.service")
    if units:
        subprocess.run(["systemctl", "stop", "fieldmap-remote", "fieldmap-share"], check=False)
    # These folders belong to the map server's user (its network services write there), and this
    # runs as root: a link in them must not lead a write anywhere else.
    top = os.path.realpath(MAPS)
    for rel, mode, data in files:                   # directories first: the tar lists them first
        target = os.path.join(MAPS, rel)
        here = os.path.join(top, rel.split("/")[0])
        if data is None:
            os.makedirs(target, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        parent = os.path.realpath(target if data is None else os.path.dirname(target))
        if os.path.islink(here) or (parent != here and not parent.startswith(here + os.sep)) or \
                (data is None and os.path.islink(target)):
            if units:                               # leave it running as it was
                subprocess.run(["systemctl", "start", "fieldmap-remote", "fieldmap-share"], check=False)
            raise RuntimeError(f"Map server: {rel} leads outside its folder (a link); restore stopped.")
        if data is not None:
            fd, tmp = tempfile.mkstemp(dir=parent, prefix=".restore-")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(parent, os.path.basename(target)))
            target = os.path.join(parent, os.path.basename(target))
        os.chown(target, st.st_uid, st.st_gid, follow_symlinks=False)
        os.chmod(target, mode & 0o777)
    if env:
        envf = os.path.join(FIELDMAP, ".env")
        try:
            with open(envf) as f:
                keep = [l.rstrip("\n") for l in f if not MAP_ENV_LINE.match(l.rstrip("\n"))]
        except OSError:
            keep = []
        tmp = envf + ".restore"
        with open(tmp, "w") as f:
            f.write("\n".join(keep + env) + "\n")
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, 0o600)
        os.replace(tmp, envf)
    if not units:
        say("Map server: pairing restored. It is used once the map server is installed (sudo ./install.sh maps).")
        return
    say("Map server: pairing restored; setting up its remote access again...")
    r = subprocess.run(["systemctl", "start", "fieldmap-remote-apply.service"], capture_output=True, text=True)
    subprocess.run(["systemctl", "restart", "fieldmap-remote", "fieldmap-share"], check=False)
    subprocess.run(["docker", "restart", "fieldmap-remote"], capture_output=True)
    if r.returncode:
        raise RuntimeError("The map server's remote access didn't start again (journalctl -u fieldmap-remote-apply).")
    if not gateway_up():
        raise RuntimeError("The map server's gateway didn't answer after the restore (docker logs fieldmap-remote).")
    say("Map server: phones paired with the backed-up box work again.")


def gateway_up(wait=30):
    """The gateway answers 400 to a visitor without a phone's certificate once it is up."""
    port = next((l.split("=", 1)[1] for l in map_env() if l.startswith("REMOTE_PORT=")), "")
    if not port.isdigit():
        return True                                 # no gateway set up yet: nothing to wait for
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    for _ in range(wait):
        try:
            urllib.request.urlopen(f"https://127.0.0.1:{port}/", context=ctx, timeout=3)
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False


def backup(drive, say):
    root = drive_path(drive)
    dest = os.path.join(root, "EmergencyBox-backups")
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(MYFILES) for f in fs if os.path.isfile(os.path.join(r, f)))
    space = free(root) or {"free": 0}
    if size + (50 << 20) > space["free"]:
        raise RuntimeError(f"Not enough room on the drive: My files is {size >> 20} MB, the drive has {space['free'] >> 20} MB free.")
    if fstype(root) == "vfat" and size > (3900 << 20):
        raise RuntimeError("This drive is FAT32, which can't hold a backup over 4 GB. Use an exFAT drive.")
    os.makedirs(dest, exist_ok=True)
    name = time.strftime("backup-%Y%m%d-%H%M.tar.gz")
    tmp = os.path.join(dest, "." + name + ".part")
    say(f"Backing up My files ({size >> 20} MB) and the box settings to {drive}...")
    with tarfile.open(tmp, "w:gz", compresslevel=3) as t:
        t.add(conf_path(), arcname="settings/box.conf")
        if add_map_state(t):
            say("Map server: pairing and sharing settings included (private keys: keep this drive safe).")
        if os.path.isdir(MYFILES):
            for n in sorted(os.listdir(MYFILES)):
                if n != "lost+found":
                    t.add(os.path.join(MYFILES, n), arcname=f"myfiles/{n}")
    os.replace(tmp, os.path.join(dest, name))
    os.sync()
    say(f"Done: {name} on {drive}. Use Eject in My files before unplugging the drive.")


RESTORE_KEYS = ("WIFI_SSID", "WIFI_PASSWORD", "USER_PASSWORD")


def restore(drive, fname, with_settings, say):
    root = drive_path(drive)
    if not re.match(r"^backup-[0-9]{8}-[0-9]{4}\.tar\.gz$", fname or ""):
        raise RuntimeError("Pick one of the backups on the drive.")
    path = os.path.join(root, "EmergencyBox-backups", fname)
    say(f"Restoring My files from {fname}...")
    n = 0
    settings = None
    map_files, env = [], []
    with tarfile.open(path, "r:gz") as t:
        for m in t:
            bits = m.name.split("/")
            if ".." in bits or m.name.startswith("/") or not (m.isfile() or m.isdir()):
                continue                                    # nothing outside, no links or devices
            if m.name == "settings/box.conf" and m.isfile():
                settings = t.extractfile(m).read().decode(errors="replace")
            elif m.name == "mapserver/env" and m.isfile() and m.size < 64 << 10:
                env = [l for l in t.extractfile(m).read().decode(errors="replace").splitlines() if MAP_ENV_LINE.match(l)]
            elif bits[0] == "mapserver" and len(bits) > 2 and bits[1] in MAP_DIRS and bits[2] != "run" and m.size < 1 << 20:
                map_files.append(("/".join(bits[1:]), m.mode, t.extractfile(m).read() if m.isfile() else None))
            elif bits[0] == "myfiles" and len(bits) > 1:
                target = os.path.join(MYFILES, *bits[1:])
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with t.extractfile(m) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, CHUNK)
                    n += 1
    os.sync()
    say(f"My files: {n} files restored.")
    if not with_settings:
        return
    # The backup comes from a USB drive: its values go into box.conf, which the installer runs
    # as root, so each one passes the same checks as on the admin page, before anything changes.
    vals = {k: v for k, v in conf_parse(settings or "").items() if k in RESTORE_KEYS}
    try:
        if vals.get("WIFI_SSID") is not None and not SSID_OK.match(vals["WIFI_SSID"]):
            raise ApiError(400, "The backup's Wi-Fi name can't be used.")
        if vals.get("WIFI_PASSWORD") is not None:
            check_pw(vals["WIFI_PASSWORD"], "Wi-Fi password in the backup", 8)
        if vals.get("USER_PASSWORD") is not None:
            check_pw(vals["USER_PASSWORD"], "user password in the backup")
    except ApiError as e:
        raise RuntimeError(e.msg + " Settings not restored.")
    if map_files or env:
        restore_map(map_files, env, say)
    if vals:
        cur = conf_get()
        if vals.get("USER_PASSWORD") and vals["USER_PASSWORD"] != cur.get("USER_PASSWORD"):
            if "movies" in parts_installed():
                jf_set_passwords(cur.get("ADMIN_PASSWORD", ""), None, cur.get("USER_NAME", "user"), vals["USER_PASSWORD"])
        conf_set(vals)
        say("Settings: Wi-Fi name and password and the user password restored; applying them (the Wi-Fi restarts)...")
        r = subprocess.run(installer("network", "portal", "landing"), cwd=INSTALLER, capture_output=True, text=True)
        say(r.stdout[-3000:])
        if r.returncode:
            raise RuntimeError("The installer couldn't apply the settings (above).")


def parts_installed():
    return set(parts())


# ---------- HTTP ------------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "boxadmin"

    def log_message(self, *a):
        pass

    def send(self, code, obj, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "boxadmin":
                return v
        return None

    def authed(self):
        t = self.token()
        with LOCK:
            exp = SESSIONS.get(t)
            if exp and exp > time.time():
                return True
            SESSIONS.pop(t, None)
        return False

    def body(self, limit=64 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ApiError(413, "Too big.")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError(400, "Bad request.")

    def route(self):
        u = urllib.parse.urlsplit(self.path)
        return u.path.rstrip("/"), dict(urllib.parse.parse_qsl(u.query))

    def do_GET(self):
        try:
            path, _ = self.route()
            if path == "/api/admin/state":
                if not self.authed():
                    return self.send(200, {"signed_in": False})
                c = conf_get()
                return self.send(200, {
                    "signed_in": True,
                    "settings": {"wifi_ssid": c.get("WIFI_SSID", ""), "wifi_password": c.get("WIFI_PASSWORD", ""),
                                 "user_name": c.get("USER_NAME", "user"), "user_password": c.get("USER_PASSWORD", "")},
                    "parts": parts(), "content": content(), "libraries": jf_libs(), "systems": game_systems(),
                    "map_layers": MAP_LAYERS, "drives": drives(), "job": job_state()})
            if path == "/api/admin/job":
                if not self.authed():
                    raise ApiError(401, "Sign in again.")
                return self.send(200, {"job": job_state()})
            raise ApiError(404, "Not found.")
        except ApiError as e:
            self.send(e.code, {"error": e.msg})

    def do_POST(self):
        try:
            path, q = self.route()
            if self.headers.get("X-Box-Admin") != "1":
                raise ApiError(403, "Missing header.")
            if path == "/api/admin/login":
                return self.login()
            if not self.authed():
                raise ApiError(401, "Sign in again.")
            if path == "/api/admin/logout":
                with LOCK:
                    SESSIONS.pop(self.token(), None)
                return self.send(200, {"ok": True}, "boxadmin=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            if path == "/api/admin/upload":
                return self.upload(q)
            b = self.body()
            if path == "/api/admin/wifi":
                return self.wifi(b)
            if path == "/api/admin/passwords":
                return self.passwords(b)
            if path == "/api/admin/upload-check":
                _, name = upload_target(b.get("kind", ""), b.get("sub", ""), b.get("name", ""), int(b.get("size") or 0))
                return self.send(200, {"ok": True, "name": name})
            if path == "/api/admin/delete":
                return self.delete(b)
            if path == "/api/admin/backup":
                d = b.get("drive")
                drive_path(d)
                start_job(f"Backup to {d}", func=lambda say: backup(d, say))
                return self.send(200, {"ok": True, "message": "Backup started."})
            if path == "/api/admin/restore":
                d, f, s = b.get("drive"), b.get("file"), bool(b.get("settings"))
                drive_path(d)
                start_job(f"Restore from {d}", func=lambda say: restore(d, f, s, say))
                return self.send(200, {"ok": True, "message": "Restore started."})
            raise ApiError(404, "Not found.")
        except ApiError as e:
            self.send(e.code, {"error": e.msg})
        except Exception as e:                  # never a stack trace to the browser; the log has it
            log(f"error: {self.path}: {e!r}")
            self.send(500, {"error": "Something went wrong on the box. Try again."})

    def login(self):
        ip = self.ip()
        with LOCK:
            n, until = FAILS.get(ip, [0, 0])
            if until > time.time():
                raise ApiError(429, f"Too many tries. Wait {int(until - time.time()) + 1} seconds.")
        pw = self.body().get("password", "")
        if not isinstance(pw, str) or not pw_ok(pw):
            with LOCK:
                n += 1
                FAILS[ip] = [n, time.time() + (30 if n >= 5 else 0)]
            log(f"login failed from {ip}")
            raise ApiError(401, "Wrong password.")
        with LOCK:
            FAILS.pop(ip, None)
            tok = new_session()
        log(f"signed in from {ip}")
        self.send(200, {"ok": True}, f"boxadmin={tok}; Path=/; Max-Age={SESSION_HOURS * 3600}; HttpOnly; SameSite=Strict")

    def wifi(self, b):
        ssid, pw = b.get("ssid", ""), b.get("password", "")
        if not isinstance(ssid, str) or not SSID_OK.match(ssid):
            raise ApiError(400, "Wi-Fi name: 1 to 32 letters, numbers, spaces and . _ - only.")
        check_pw(pw, "Wi-Fi password", 8)
        if job_busy():
            raise ApiError(409, "Something else is still running. Wait for it to finish.")
        conf_set({"WIFI_SSID": ssid, "WIFI_PASSWORD": pw})
        start_job("Applying the new Wi-Fi settings", installer("network", "portal", "landing"))
        log(f"wifi changed to {ssid}")
        self.send(200, {"ok": True, "message": f'The Wi-Fi restarts in a few seconds as "{ssid}". Join it with the new password, then open the box again.'})

    def passwords(self, b):
        cur, new_admin, new_user = b.get("current", ""), b.get("admin") or None, b.get("user") or None
        if not isinstance(cur, str) or not pw_ok(cur):
            raise ApiError(401, "The current admin password is wrong.")
        if new_admin:
            check_pw(new_admin, "admin password", 8)
        if new_user:
            check_pw(new_user, "user password")
        if not (new_admin or new_user):
            raise ApiError(400, "Nothing to change.")
        if new_user and job_busy():
            raise ApiError(409, "Something else is still running. Wait for it to finish.")
        c = conf_get()
        have = parts_installed()
        if "movies" in have:
            jf_set_passwords(cur, new_admin, c.get("USER_NAME", "user"), new_user)
        if new_admin and "books" in have:
            calibre_set_admin(new_admin)
        changes = {}
        if new_admin:
            changes["ADMIN_PASSWORD"] = new_admin
            tmp = PASSFILE + ".tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write(hash_pw(new_admin))
            os.replace(tmp, PASSFILE)
        if new_user:
            changes["USER_PASSWORD"] = new_user
        conf_set(changes)
        log("passwords changed: " + ", ".join(k for k in changes))
        msg = "Saved."
        if new_user:
            start_job("Updating the sign-in shown on the home page", installer("landing"))
            msg = "Saved. The home page shows the new user password in a moment."
        self.send(200, {"ok": True, "message": msg})

    def upload(self, q):
        n = int(self.headers.get("Content-Length") or -1)
        try:
            if n < 0:
                raise ApiError(411, "Size missing.")
            d, name = upload_target(q.get("kind", ""), q.get("sub", ""), q.get("name", ""), n)
        except ApiError:
            self.drain(n)
            raise
        kind, sub = q["kind"], q.get("sub", "")
        first = kind == "movies" and name.lower().endswith(VIDEOS) and not has_video(kind_dir(kind, sub.partition("/")[0]))
        os.makedirs(d, exist_ok=True)
        final = os.path.join(d, name)
        tmp = os.path.join(d, f".{name}.upload-{secrets.token_hex(4)}")
        left = n
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        raise ApiError(400, "The upload stopped part way.")
                    f.write(chunk)
                    left -= len(chunk)
                f.flush(); os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, final)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        log(f"upload {kind}/{sub}/{name} ({n} bytes)")
        if first:
            jf_scan()
        msg = after_change(kind)
        self.send(200, {"ok": True, "message": msg, "name": name})

    def drain(self, n, limit=64 << 20):
        """Read what the browser is still sending, so nginx gets our answer and not a reset (502).
        Above the limit, the connection is just closed: the page checks before it sends (upload-check)."""
        left = min(n, limit) if n > 0 else 0
        while left > 0:
            chunk = self.rfile.read(min(CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
        if n > limit:
            self.close_connection = True

    def delete(self, b):
        kind, sub, name = b.get("kind", ""), b.get("sub", ""), b.get("name", "")
        if not isinstance(sub, str) or not isinstance(name, str) or not name:
            raise ApiError(400, "Bad request.")
        lib = sub.partition("/")[0] if kind == "movies" else sub
        d = kind_dir(kind, lib)
        p = os.path.realpath(os.path.join(d, name))
        if b.get("folder") is True:                         # a whole movie, show or season folder
            if kind != "movies" or not p.startswith(os.path.realpath(d) + os.sep) or not os.path.isdir(p):
                raise ApiError(404, "That folder isn't there.")
            shutil.rmtree(p)
            parent = os.path.dirname(p)
            while parent != os.path.realpath(d) and not os.listdir(parent):
                os.rmdir(parent); parent = os.path.dirname(parent)
            log(f"delete folder {kind}/{sub}/{name}")
            return self.send(200, {"ok": True, "message": ("Deleted. " + after_change(kind)).strip()})
        if not p.startswith(os.path.realpath(d) + os.sep) or not os.path.isfile(p):
            raise ApiError(404, "That file isn't there.")
        # The same files the page lists and uploads: their endings, and for maps only the layers
        # at the top (the map server's own data, keys and routing sit beside them)
        ends = KINDS[kind][1]
        if (ends and not p.lower().endswith(ends)) or \
                (kind == "maps" and (os.path.dirname(p) != os.path.realpath(d) or os.path.basename(p)[:-len(".mbtiles")] not in MAP_LAYERS)):
            raise ApiError(403, "That file can't be deleted here.")
        os.remove(p)
        parent = os.path.dirname(p)                         # an emptied show/movie folder goes too
        while parent != os.path.realpath(d) and not os.listdir(parent):
            os.rmdir(parent); parent = os.path.dirname(parent)
        log(f"delete {kind}/{sub}/{name}")
        self.send(200, {"ok": True, "message": ("Deleted. " + after_change(kind)).strip()})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    os.makedirs(os.path.dirname(JOBDIR), exist_ok=True)
    print(f"boxadmin on 127.0.0.1:{PORT} (installer {INSTALLER}, content {CONTENT_RW})", flush=True)
    Server(("127.0.0.1", PORT), Handler).serve_forever()
