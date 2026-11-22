#!/usr/bin/env python3
"""boxfiles - tiny file manager API for the Emergency Box.

Roots:
  my      -> MYFILES_DIR, a plain folder on the system disk; at most MAX_GB (0 = no own limit)
  library -> LIBRARY_DIR (Jellyfin's library folders), uploads into a library folder only
  usbN    -> /media/usbN, only while something is really mounted there (box-usb)
On the system disk, writes stop when free space would drop under RESERVE_GB, so My files and
uploads can never fill the disk the box runs from.

Python standard library only. Listens on 127.0.0.1; nginx proxies /api/files/.
Large downloads are handed back to nginx with X-Accel-Redirect.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("BOXFILES_HOST", "127.0.0.1")
PORT = int(os.environ.get("BOXFILES_PORT", "8182"))
MYFILES_DIR = os.environ.get("BOXFILES_MY", "/srv/myfiles")
MEDIA_DIR = os.environ.get("BOXFILES_MEDIA", "/media")
# Movies & shows: the folder that holds Jellyfin's library folders (empty = not offered).
# Uploads go into a library folder, never loose at the top, where Jellyfin wouldn't see them.
LIBRARY_DIR = os.environ.get("BOXFILES_LIBRARY", "")
LIBRARY_NAMES = dict(x.split(":", 1) for x in os.environ.get("BOXFILES_LIBRARY_NAMES", "").split() if ":" in x)
GB = 1024 ** 3
MAX_MY = int(os.environ.get("BOXFILES_MAX_GB", "5") or 0) * GB
RESERVE = int(os.environ.get("BOXFILES_RESERVE_GB", "5") or 0) * GB
# Refuse to touch a USB root unless it is a real mount point (never fill the system disk
# through an empty /media/usbN folder).
REQUIRE_MOUNT = os.environ.get("BOXFILES_REQUIRE_MOUNT", "1") == "1"
# nginx internal locations used for downloads (see boxfiles-nginx.conf)
ACCEL_MY = os.environ.get("BOXFILES_ACCEL_MY", "/_boxfiles_my/")
ACCEL_MEDIA = os.environ.get("BOXFILES_ACCEL_MEDIA", "/_boxfiles_media/")
ACCEL_LIBRARY = os.environ.get("BOXFILES_ACCEL_LIBRARY", "/_boxfiles_library/")
USE_ACCEL = os.environ.get("BOXFILES_ACCEL", "1") == "1"

MAX_TEXT = 2 * 1024 * 1024          # editor limit
CHUNK = 1024 * 1024
HIDDEN = {"lost+found", "System Volume Information", "$RECYCLE.BIN", ".Trashes",
          ".Spotlight-V100", ".fseventsd", ".Trash-1000"}
BAD_NAME = re.compile(r'[\x00-\x1f/\\<>:"|?*]')   # also unsafe on FAT/exFAT/NTFS
USB_RE = re.compile(r"^usb[0-9]+$")
ACTIVE = re.compile(r"\.(html?|xhtml|svgz?|xml|xsl|js|mjs)$", re.I)
_copy_lock = threading.Lock()
# My files' size: walking the whole folder on every listing is slow with many files. Kept for a
# minute; every change made through boxfiles clears it (copies made elsewhere show within a minute).
_my_size = {"t": 0.0, "v": 0}
_my_size_lock = threading.Lock()
MY_SIZE_TTL = 60


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status
        self.msg = msg


# ---------- roots ----------

def _mounts():
    out = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    mp = p[1].replace("\\040", " ")
                    out[mp] = {"dev": p[0], "fs": p[2], "opts": p[3].split(",")}
    except OSError:
        pass
    return out


def _is_mount(path):
    """True if path is a mount point per /proc/mounts (never os.path.ismount, which can't see
    bind mounts)."""
    return os.path.realpath(path) in _mounts()


def _labels():
    """device path -> filesystem label, from /dev/disk/by-label."""
    res = {}
    d = "/dev/disk/by-label"
    try:
        for name in os.listdir(d):
            dev = os.path.realpath(os.path.join(d, name))
            res[dev] = name.encode().decode("unicode_escape", "ignore") if "\\x" in name else name
    except OSError:
        pass
    return res


def _disk(path):
    st = os.statvfs(path)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def _within(path, top):
    return bool(top) and (path == top or path.startswith(top.rstrip("/") + "/"))


def _usage(path):
    """(total, free) as the family sees them. USB drives: the drive. My files: its own limit
    (MAX_MY) and the system disk's reserve. Movies & shows: the system disk minus the reserve."""
    total, free = _disk(path)
    p = os.path.realpath(path)
    if _within(p, MYFILES_DIR) or _within(p, LIBRARY_DIR):
        free = max(0, free - RESERVE)
        if _within(p, MYFILES_DIR) and MAX_MY:
            used = _my_used()
            free = max(0, min(free, MAX_MY - used))
            total = MAX_MY
    return total, free


def roots():
    mounts = _mounts()
    out = []
    if os.path.isdir(MYFILES_DIR):
        total, free = _usage(MYFILES_DIR)
        out.append({"id": "my", "name": "My files", "kind": "my",
                    "total": total, "free": free, "readonly": False})
    else:
        out.append({"id": "my", "name": "My files", "kind": "my", "missing": True})
    if LIBRARY_DIR and os.path.isdir(LIBRARY_DIR):
        total, free = _usage(LIBRARY_DIR)
        out.append({"id": "library", "name": "Movies & shows", "kind": "library",
                    "total": total, "free": free, "readonly": not os.access(LIBRARY_DIR, os.W_OK)})
    labels = _labels()
    try:
        names = sorted(os.listdir(MEDIA_DIR), key=lambda n: int(n[3:]) if USB_RE.match(n) else 999)
    except OSError:
        names = []
    for n in names:
        if not USB_RE.match(n):
            continue
        mp = os.path.join(MEDIA_DIR, n)
        m = mounts.get(mp)
        if REQUIRE_MOUNT and not m:
            continue
        if not os.path.isdir(mp):
            continue
        total, free = _usage(mp)
        label = labels.get(m["dev"]) if m else None
        out.append({"id": n, "name": label or "USB drive", "kind": "usb",
                    "slot": int(n[3:]), "fs": m["fs"] if m else "",
                    "total": total, "free": free,
                    "readonly": bool(m and "ro" in m["opts"])})
    return out


def root_dir(root_id):
    if root_id == "my":
        d = MYFILES_DIR
    elif root_id == "library" and LIBRARY_DIR:
        d = LIBRARY_DIR
        if not os.path.isdir(d):
            raise ApiError(503, "Movies & shows storage is not available.")
        return d
    elif root_id and USB_RE.match(root_id):
        d = os.path.join(MEDIA_DIR, root_id)
    else:
        raise ApiError(400, "Unknown storage.")
    if not os.path.isdir(d) or (root_id != "my" and REQUIRE_MOUNT and not _is_mount(d)):
        if root_id == "my":
            raise ApiError(503, "My files storage is not available.")
        raise ApiError(404, "That USB drive is not plugged in any more.")
    return d


def check_name(name):
    if name is None:
        raise ApiError(400, "Missing name.")
    name = name.strip()
    if not name or name in (".", "..") or BAD_NAME.search(name) or len(name.encode()) > 240:
        raise ApiError(400, 'That name can\'t be used. Avoid / \\ : * ? " < > | and keep it short.')
    if name.endswith(".") or name.endswith(" "):
        raise ApiError(400, "Names can't end with a dot or a space.")
    return name


def resolve(root_id, rel, must_exist=True):
    """Return (root_path, absolute_path) with traversal and symlink escapes refused."""
    base = os.path.realpath(root_dir(root_id))
    rel = (rel or "").strip("/")
    parts = [p for p in rel.split("/") if p]
    for p in parts:
        if p in (".", ".."):
            raise ApiError(400, "Bad path.")
    full = os.path.join(base, *parts) if parts else base
    real = os.path.realpath(full)
    if real != base and not real.startswith(base + os.sep):
        raise ApiError(403, "Outside the allowed storage.")
    if must_exist and not os.path.lexists(full):
        raise ApiError(404, "Not found. It may have been moved or deleted.")
    return base, real


def rel_of(base, path):
    r = os.path.relpath(path, base)
    return "" if r == "." else r


def unique_path(dirpath, name):
    stem, ext = os.path.splitext(name)
    if name.startswith(".") and ext == name:
        stem, ext = name, ""
    cand = os.path.join(dirpath, name)
    i = 1
    while os.path.lexists(cand):
        cand = os.path.join(dirpath, f"{stem} ({i}){ext}")
        i += 1
    return cand


# ---------- operations ----------

def lib_guard(root_id, base, parent, moving=None):
    """Movies & shows: nothing loose at the top (Jellyfin only reads its library folders),
    and the library folders themselves stay put."""
    if root_id != "library":
        return
    if moving is not None and os.path.dirname(moving) == base:
        raise ApiError(400, "The library folders themselves can't be renamed, moved or deleted.")
    if parent is not None and os.path.normpath(parent) == os.path.normpath(base):
        names = ", ".join(LIBRARY_NAMES.values()) or "the library folders"
        raise ApiError(400, "Open one of the folders first (%s) so Jellyfin finds it." % names)


def op_list(root_id, rel):
    base, d = resolve(root_id, rel)
    if not os.path.isdir(d):
        raise ApiError(400, "Not a folder.")
    items = []
    with os.scandir(d) as it:
        for e in it:
            if e.name in HIDDEN or e.name.startswith(".") or e.name.startswith(".upload-"):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if e.is_symlink():
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            it_ = {"name": e.name, "dir": is_dir, "size": 0 if is_dir else st.st_size, "mtime": int(st.st_mtime)}
            if root_id == "library" and d == base and e.name in LIBRARY_NAMES:
                it_["label"] = LIBRARY_NAMES[e.name]       # Jellyfin's name for the folder
            items.append(it_)
    items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"root": root_id, "path": rel_of(base, d), "items": items}


def op_mkdir(root_id, rel, name):
    name = check_name(name)
    base, d = resolve(root_id, rel)
    if not os.path.isdir(d):
        raise ApiError(400, "Not a folder.")
    lib_guard(root_id, base, d)
    target = os.path.join(d, name)
    if os.path.lexists(target):
        raise ApiError(409, "Something with that name is already here.")
    os.mkdir(target)
    return {"ok": True}


def op_rename(root_id, rel, new):
    new = check_name(new)
    base, p = resolve(root_id, rel)
    if p == base:
        raise ApiError(400, "Can't rename the drive itself.")
    lib_guard(root_id, base, None, moving=p)
    target = os.path.join(os.path.dirname(p), new)
    if os.path.lexists(target) and os.path.normcase(target) != os.path.normcase(p):
        raise ApiError(409, "Something with that name is already here.")
    if target == p:
        return {"ok": True}
    # case-only rename on FAT: go through a temporary name
    if os.path.lexists(target):
        tmp = p + ".renaming"
        os.rename(p, tmp)
        os.rename(tmp, target)
    else:
        os.rename(p, target)
    return {"ok": True}


def op_delete(root_id, rel):
    base, p = resolve(root_id, rel)
    if p == base:
        raise ApiError(400, "Can't delete the drive itself.")
    lib_guard(root_id, base, None, moving=p)
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    else:
        os.remove(p)
    _sync()
    return {"ok": True}


def op_read_text(root_id, rel):
    base, p = resolve(root_id, rel)
    if not os.path.isfile(p):
        raise ApiError(400, "Not a file.")
    if os.path.getsize(p) > MAX_TEXT:
        raise ApiError(413, "Too big to edit here (limit 2 MB). Download it instead.")
    with open(p, "rb") as f:
        data = f.read()
    if b"\x00" in data[:8192]:
        raise ApiError(415, "This isn't a text file.")
    return {"text": data.decode("utf-8", "replace")}


def op_save_text(root_id, rel, text, create=False):
    if text is None:
        raise ApiError(400, "Missing text.")
    data = text.encode("utf-8")
    if len(data) > MAX_TEXT:
        raise ApiError(413, "Too big to save from the editor (limit 2 MB).")
    if create:
        parent_rel, _, name = (rel or "").rstrip("/").rpartition("/")
        name = check_name(name)
        base, d = resolve(root_id, parent_rel)
        lib_guard(root_id, base, d)
        p = os.path.join(d, name)
        if os.path.lexists(p):
            raise ApiError(409, "Something with that name is already here.")
    else:
        base, p = resolve(root_id, rel)
        if not os.path.isfile(p):
            raise ApiError(400, "Not a file.")
    d = os.path.dirname(p)
    _need_space(d, len(data) - (os.path.getsize(p) if os.path.exists(p) else 0))
    fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _readable(tmp)
        os.replace(tmp, p)
    except BaseException:
        _silent_remove(tmp)
        raise
    return {"ok": True}


def _tree_size(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    total = 0
    for dp, dn, fn in os.walk(p):
        for n in fn:
            try:
                total += os.path.getsize(os.path.join(dp, n))
            except OSError:
                pass
    return total


def _my_used():
    with _my_size_lock:
        if time.monotonic() - _my_size["t"] > MY_SIZE_TTL:
            _my_size.update(v=_tree_size(MYFILES_DIR), t=time.monotonic())
        return _my_size["v"]


def _my_size_changed():
    with _my_size_lock:
        _my_size["t"] = 0.0


def _need_space(d, nbytes):
    if nbytes <= 0:
        return
    _, free = _usage(d)
    if nbytes > free - 1024 * 1024:
        if _within(os.path.realpath(d), MYFILES_DIR) and MAX_MY:
            raise ApiError(507, f"My files is full: needs {_fmt(nbytes)}, only {_fmt(max(free, 0))} left. Move files to a USB drive.")
        raise ApiError(507, f"Not enough space: needs {_fmt(nbytes)}, only {_fmt(max(free, 0))} free.")


def _fmt(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def _fat_limit(d, nbytes):
    m = _mount_of(d)
    if m and m["fs"] in ("vfat", "msdos") and nbytes >= 4 * 1024 ** 3:
        raise ApiError(413, "This USB drive is formatted FAT32, which can't hold files of 4 GB or more.")


def _mount_of(path):
    path = os.path.realpath(path)
    mounts = _mounts()
    best = None
    for mp, m in mounts.items():
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best[0]):
                best = (mp, m)
    return best[1] if best else None


def op_copy(src_root, src_rel, dst_root, dst_rel, move=False):
    sbase, s = resolve(src_root, src_rel)
    if s == sbase:
        raise ApiError(400, "Pick a file or folder, not the whole drive.")
    dbase, d = resolve(dst_root, dst_rel)
    if not os.path.isdir(d):
        raise ApiError(400, "The destination is not a folder.")
    lib_guard(dst_root, dbase, d)
    if move:
        lib_guard(src_root, sbase, None, moving=s)
    if os.path.isdir(s) and (d == s or d.startswith(s + os.sep)):
        raise ApiError(400, "Can't put a folder inside itself.")
    name = os.path.basename(s)
    target = unique_path(d, name)
    same_fs = os.stat(s).st_dev == os.stat(d).st_dev
    if move and same_fs:
        os.rename(s, target)
        return {"ok": True, "name": os.path.basename(target)}
    size = _tree_size(s)
    _need_space(d, size)
    if os.path.isfile(s):
        _fat_limit(d, size)
    with _copy_lock:
        tmp = target + ".partial" if os.path.isfile(s) else target
        try:
            if os.path.isdir(s):
                shutil.copytree(s, tmp, copy_function=shutil.copy2)
            else:
                shutil.copy2(s, tmp)
                os.replace(tmp, target)
        except BaseException:
            if os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                _silent_remove(tmp)
            raise
        if move:
            if os.path.isdir(s):
                shutil.rmtree(s)
            else:
                os.remove(s)
        _sync()
    return {"ok": True, "name": os.path.basename(target)}


def op_eject(root_id):
    if not (root_id and USB_RE.match(root_id)):
        raise ApiError(400, "Only USB drives can be ejected.")
    mp = root_dir(root_id)
    _sync()
    if not REQUIRE_MOUNT:          # test mode: nothing to unmount
        return {"ok": True}
    r = subprocess.run(["umount", mp], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        time.sleep(2)
        _sync()
        r = subprocess.run(["umount", mp], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ApiError(409, "The drive is busy (something is still using it). Wait a moment and try again.")
    return {"ok": True}


def _sync():
    try:
        os.sync()
    except Exception:
        pass


def _readable(p):
    try:
        os.chmod(p, 0o644)
    except OSError:
        pass   # FAT/exFAT/NTFS: permissions come from the mount options


def _silent_remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "boxfiles/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    # helpers
    def _q(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
        return u.path, q

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_TEXT + 65536:
            raise ApiError(413, "Request too large.")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Bad request.")

    def _run(self, fn):
        if self.command != "GET":
            _my_size_changed()                   # before: this request's space check sees the latest size
        try:
            fn()
        except ApiError as e:
            self._drain()
            self._json(e.status, {"error": e.msg})
        except PermissionError:
            self._drain()
            self._json(403, {"error": "The box isn't allowed to change that (the drive may be read-only)."})
        except OSError as e:
            self._drain()
            msg = "The drive reported an error: %s." % (e.strerror or "unknown")
            if e.errno == 28:
                msg = "The drive is full."
            elif e.errno == 30:
                msg = "This drive is read-only."
            self._json(500, {"error": msg})
        except Exception as e:  # noqa
            self._drain()
            self._json(500, {"error": "Unexpected error: %s" % e})
        finally:
            if self.command != "GET":
                _my_size_changed()               # and after: the next listing counts what it added

    def _drain(self):
        # Discard an unread request body so the connection stays usable.
        if getattr(self, "_body_done", False):
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            while n > 0:
                chunk = self.rfile.read(min(CHUNK, n))
                if not chunk:
                    break
                n -= len(chunk)
        except Exception:
            pass
        self._body_done = True

    # routes
    def do_GET(self):
        path, q = self._q()

        def go():
            if path.endswith("/roots"):
                self._body_done = True
                self._json(200, {"roots": roots()})
            elif path.endswith("/list"):
                self._body_done = True
                self._json(200, op_list(q.get("root"), q.get("path", "")))
            elif path.endswith("/text"):
                self._body_done = True
                self._json(200, op_read_text(q.get("root"), q.get("path", "")))
            elif path.endswith("/get"):
                self._body_done = True
                self._download(q.get("root"), q.get("path", ""), q.get("dl") == "1")
            else:
                raise ApiError(404, "No such action.")
        self._run(go)

    def do_PUT(self):
        path, q = self._q()

        def go():
            if not path.endswith("/upload"):
                raise ApiError(404, "No such action.")
            self._upload(q.get("root"), q.get("path", ""), q.get("name"))
        self._run(go)

    def do_POST(self):
        path, _ = self._q()

        def go():
            b = self._body_json()
            self._body_done = True
            act = path.rstrip("/").rsplit("/", 1)[-1]
            if act == "mkdir":
                r = op_mkdir(b.get("root"), b.get("path", ""), b.get("name"))
            elif act == "rename":
                r = op_rename(b.get("root"), b.get("path", ""), b.get("name"))
            elif act == "delete":
                r = op_delete(b.get("root"), b.get("path", ""))
            elif act == "save":
                r = op_save_text(b.get("root"), b.get("path", ""), b.get("text"), bool(b.get("create")))
            elif act == "copy":
                r = op_copy(b.get("root"), b.get("path", ""), b.get("toRoot"), b.get("toPath", ""),
                            bool(b.get("move")))
            elif act == "eject":
                r = op_eject(b.get("root"))
            else:
                raise ApiError(404, "No such action.")
            self._json(200, r)
        self._run(go)

    def _download(self, root_id, rel, attach):
        base, p = resolve(root_id, rel)
        if not os.path.isfile(p):
            raise ApiError(400, "Not a file.")
        name = os.path.basename(p)
        # never render user-supplied web pages on the box's own origin
        if ACTIVE.search(name):
            attach = True
        disp = "attachment" if attach else "inline"
        cd = "%s; filename*=UTF-8''%s" % (disp, urllib.parse.quote(name))
        if USE_ACCEL:
            r = rel_of(base, p)
            if root_id == "my":
                loc = ACCEL_MY + urllib.parse.quote(r)
            elif root_id == "library":
                loc = ACCEL_LIBRARY + urllib.parse.quote(r)
            else:
                loc = ACCEL_MEDIA + urllib.parse.quote(root_id + "/" + r)
            self.send_response(200)
            self.send_header("X-Accel-Redirect", loc)
            self.send_header("Content-Disposition", cd)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", cd)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, CHUNK)

    def _upload(self, root_id, rel, name):
        name = check_name(name)
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise ApiError(411, "Upload needs a known size.")
        length = int(self.headers.get("Content-Length") or -1)
        if length < 0:
            raise ApiError(411, "Upload needs a known size.")
        base, d = resolve(root_id, rel)
        if not os.path.isdir(d):
            raise ApiError(400, "Not a folder.")
        lib_guard(root_id, base, d)
        _need_space(d, length)
        _fat_limit(d, length)
        fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=d)
        left = length
        try:
            with os.fdopen(fd, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        raise ApiError(400, "The upload was interrupted.")
                    f.write(chunk)
                    left -= len(chunk)
                f.flush()
                os.fsync(f.fileno())
            self._body_done = True
            target = unique_path(d, name)
            _readable(tmp)
            os.replace(tmp, target)
        except BaseException:
            self._body_done = left <= 0
            _silent_remove(tmp)
            raise
        self._json(200, {"ok": True, "name": os.path.basename(target)})


def cleanup_partials():
    """Remove leftovers of interrupted uploads/copies in My files at start-up."""
    for top in (MYFILES_DIR, LIBRARY_DIR):
        try:
            if not top or not os.path.isdir(top):
                continue
            for dp, dn, fn in os.walk(top):
                for n in fn:
                    if n.startswith(".upload-") or n.endswith(".partial"):
                        _silent_remove(os.path.join(dp, n))
        except Exception:
            pass


def main():
    cleanup_partials()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("boxfiles on %s:%d (my=%s media=%s)\n" % (HOST, PORT, MYFILES_DIR, MEDIA_DIR))
    srv.serve_forever()


if __name__ == "__main__":
    main()
