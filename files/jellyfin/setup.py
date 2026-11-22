#!/usr/bin/env python3
"""First-run setup of Jellyfin through its own API, so nobody has to click through the wizard.

  - finishes the startup wizard with the admin account (ADMIN_PASSWORD)
  - creates the shared account USER_NAME / USER_PASSWORD
  - user: no playback that needs transcoding (direct play only; a Pi can't transcode)
  - admin: hidden from the sign-in screen
  - one library per JELLYFIN_LIBRARIES entry, under MEDIA_DIR
  - low-power machines (JF_LOW_POWER=1): one transcoding thread
  - the address prefix (JF_BASEURL, e.g. /jellyfin) through the network settings API

Exit codes: 0 done; 10 done, but the address changed and Jellyfin must be restarted;
11 done, but this Jellyfin has no network settings API (the installer writes network.xml instead).

Safe to run again: every step checks first. Standard library only.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("JF_URL", "http://127.0.0.1:8096")
WANT_BASEURL = os.environ.get("JF_BASEURL", "")
ADMIN, ADMIN_PW = "admin", os.environ["ADMIN_PASSWORD"]
USER, USER_PW = os.environ["USER_NAME"], os.environ["USER_PASSWORD"]
MEDIA = os.environ.get("MEDIA_DIR", "/srv/media")
LIBS = os.environ.get("JELLYFIN_LIBRARIES", "")
LOW_POWER = os.environ.get("JF_LOW_POWER") == "1"
CLIENT = ('MediaBrowser Client="EmergencyBox installer", Device="installer", '
          'DeviceId="emergency-box-installer", Version="1.0"')
KIND = {"shows": "tvshows", "tv": "tvshows", "youtube": "homevideos", "music": "music"}


def say(msg):
    print("         " + msg, flush=True)


def call(method, path, body=None, token=None, quiet=False):
    auth = CLIENT + (f', Token="{token}"' if token else "")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Authorization": auth,
                                          "X-Emby-Authorization": auth})
    for attempt in range(60):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = r.read().decode("utf-8", "replace")
                return json.loads(txt) if txt.strip() else None
        except urllib.error.HTTPError as e:
            if e.code == 503 and attempt < 59:      # still starting (Jellyfin's startup server)
                time.sleep(3)
                continue
            if quiet:
                raise
            sys.exit(f"Jellyfin said HTTP {e.code} to {method} {path}: {e.read()[:300]!r}")


def wait_for_server():
    """Wait for the real server. While Jellyfin starts, a temporary server answers
    /System/Info/Public (always saying setup isn't done) and 503 for everything else."""
    for _ in range(120):
        try:
            info = call("GET", "/System/Info/Public", quiet=True)
            try:
                call("GET", "/System/Ping", quiet=True)
            except urllib.error.HTTPError as e:
                if e.code == 503:
                    raise
            return info
        except Exception:
            time.sleep(3)
    sys.exit(f"Jellyfin isn't answering at {BASE}")


def main():
    info = wait_for_server()
    say(f"Jellyfin {info.get('Version', '?')} is up")

    if not info.get("StartupWizardCompleted"):
        call("POST", "/Startup/Configuration",
             {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
        call("GET", "/Startup/User")
        call("POST", "/Startup/User", {"Name": ADMIN, "Password": ADMIN_PW})
        call("POST", "/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
        call("POST", "/Startup/Complete")
        say("startup wizard finished")
        time.sleep(2)

    auth = None
    for attempt in range(6):            # right after the wizard, sign-in can take a few seconds
        try:
            auth = call("POST", "/Users/AuthenticateByName", {"Username": ADMIN, "Pw": ADMIN_PW}, quiet=True)
            break
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt >= 2:
                sys.exit(f"Can't sign in as {ADMIN}. Was the wizard finished by hand with another password? "
                         f"Then set it to ADMIN_PASSWORD in Jellyfin, or finish the setup in the browser.")
            time.sleep(3)
        except OSError:
            time.sleep(3)
    if auth is None:
        sys.exit(f"Can't sign in as {ADMIN}")
    token = auth["AccessToken"]

    users = {u["Name"]: u for u in call("GET", "/Users", token=token)}
    if USER not in users:
        call("POST", "/Users/New", {"Name": USER, "Password": USER_PW}, token=token)
        say(f"created {USER}")
        users = {u["Name"]: u for u in call("GET", "/Users", token=token)}

    def set_policy(name, **changes):
        u = users[name]
        pol = dict(u["Policy"])
        if all(pol.get(k) == v for k, v in changes.items()):
            return False
        pol.update(changes)
        call("POST", f"/Users/{u['Id']}/Policy", pol, token=token)
        return True

    if set_policy(USER, EnableVideoPlaybackTranscoding=False, EnableAudioPlaybackTranscoding=False,
                  IsHidden=False, IsAdministrator=False):
        say(f"{USER}: direct play only")
    if set_policy(ADMIN, IsHidden=True):
        say(f"{ADMIN}: hidden from the sign-in screen")

    def library_paths():
        out = {}
        for f in call("GET", "/Library/VirtualFolders", token=token) or []:
            for loc in f.get("Locations") or []:
                out[os.path.normpath(loc)] = f
        return out

    wanted = []
    for entry in LIBS.split():
        folder, _, name = entry.partition(":")
        wanted.append((folder, name or folder.capitalize(), os.path.normpath(os.path.join(MEDIA, folder))))

    # 1. Same folder under another name: rename (don't add a second library)
    by_path = library_paths()
    for folder, name, path in wanted:
        f = by_path.get(path)
        if f and f["Name"] != name:
            q = urllib.parse.urlencode({"name": f["Name"], "newName": name, "refreshLibrary": "false"})
            call("POST", f"/Library/VirtualFolders/Name?{q}", token=token)
            say(f"library {f['Name']} renamed to {name}")

    # 2. Missing libraries
    by_path = library_paths()
    have = {f["Name"] for f in by_path.values()}
    for folder, name, path in wanted:
        if path in by_path or name in have:
            continue
        q = urllib.parse.urlencode({"name": name, "collectionType": KIND.get(folder, "movies"),
                                    "paths": path, "refreshLibrary": "false"})
        call("POST", f"/Library/VirtualFolders?{q}", {"LibraryOptions": {"EnableRealtimeMonitor": True}}, token=token)
        say(f"library {name} -> {path}")

    # 3. Watch each folder for new files (uploads show up without a manual scan). A rename gives a
    #    library a new id, so this uses a fresh list; it's a convenience, so a refusal only warns.
    by_path = library_paths()
    for folder, name, path in wanted:
        f = by_path.get(path)
        opts = (f or {}).get("LibraryOptions") or {}
        if not f or not f.get("ItemId") or opts.get("EnableRealtimeMonitor") is not False:
            continue
        opts["EnableRealtimeMonitor"] = True
        try:
            call("POST", "/Library/VirtualFolders/LibraryOptions", {"Id": f["ItemId"], "LibraryOptions": opts},
                 token=token, quiet=True)
            say(f"library {name}: watches its folder for new files")
        except urllib.error.HTTPError as e:
            say(f"WARN library {name}: couldn't turn on folder watching (HTTP {e.code}); new files appear at the next scheduled scan")

    if LOW_POWER:
        enc = call("GET", "/System/Configuration/encoding", token=token)
        if enc.get("EncodingThreadCount") != 1:
            enc["EncodingThreadCount"] = 1
            call("POST", "/System/Configuration/encoding", enc, token=token)
            say("one transcoding thread (low-power machine)")

    call("POST", "/Library/Refresh", token=token)
    say("library scan started")

    if WANT_BASEURL:
        try:
            net = call("GET", "/System/Configuration/network", token=token, quiet=True)
        except urllib.error.HTTPError as e:
            say(f"no network settings API (HTTP {e.code}); the installer sets the address instead")
            sys.exit(11)
        changed = False
        if net.get("BaseUrl", "") != WANT_BASEURL:
            net["BaseUrl"] = WANT_BASEURL; changed = True
        if net.get("EnableUPnP"):
            net["EnableUPnP"] = False; changed = True
        if changed:
            call("POST", "/System/Configuration/network", net, token=token)
            say(f"address set to {WANT_BASEURL} (takes effect after a restart)")
            sys.exit(10)


if __name__ == "__main__":
    main()
