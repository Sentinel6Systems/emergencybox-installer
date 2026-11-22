#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EmergencyBox captive portal (boxportal).

The box's nginx sends the phones' "is there internet?" checks (connectivitycheck.gstatic.com,
captive.apple.com, msftconnecttest.com, ...) to this app (boxportal-server.py). Instead of opening the landing page inside the phone's limited sign-in
window, it shows one short message: "You're connected - open your browser and go to box.lan".

When the user taps OK (or Copy), the device is remembered, and from then on its checks get
the "online" answer, so the sign-in window closes and stops coming back. A device that has
not been seen for GREETED_HOURS sees the message again the next time it joins.

Python standard library only. State: SQLite in $STATE_DIRECTORY (or this folder).
"""
import html
import os
import sqlite3
import time

ADDRESS = "{{ADDRESS}}"                 # what the user should type in their browser
IP = "{{HOTSPOT_IP}}"                   # the same, for phones whose DNS goes elsewhere
NAME = "{{BOX_NAME}}"
SUBTITLE = "{{BOX_SUBTITLE}}"
GREETED_HOURS = 24 * 365          # remembered for a year
MARKER = "boxportal"                # installer uses this to recognise this file

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIRECTORY") or HERE
DB = os.path.join(STATE_DIR.split(":")[0], "boxportal.sqlite")

APPLE_HOSTS = {"captive.apple.com", "appleiphonecell.com", "www.appleiphonecell.com",
               "apple.com", "www.apple.com", "gsp1.apple.com", "thinkdifferent.us",
               "www.thinkdifferent.us", "www.itools.info", "www.ibook.info", "www.airport.us",
               "init-p01st.push.apple.com"}
APPLE_SUCCESS = b"<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"


# ---------------------------------------------------------------- state
def _db():
    con = sqlite3.connect(DB, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS devices (k TEXT PRIMARY KEY, greeted REAL, seen REAL)")
    return con


def _mac(ip):
    try:
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                if len(p) >= 4 and p[0] == ip and p[3] != "00:00:00:00:00:00":
                    return p[3].lower()
    except OSError:
        pass
    return None


def _key(environ):
    ip = (environ.get("HTTP_X_FORWARDED_FOR") or environ.get("REMOTE_ADDR") or "").split(",")[0].strip()
    mac = _mac(ip)
    return ("mac:" + mac) if mac else ("ip:" + ip)


def is_greeted(k):
    """True if this device tapped OK and has been seen within GREETED_HOURS. Updates 'seen'."""
    now = time.time()
    try:
        con = _db()
        with con:
            row = con.execute("SELECT greeted, seen FROM devices WHERE k=?", (k,)).fetchone()
            ok = bool(row and row[0] and now - (row[1] or 0) < GREETED_HOURS * 3600)
            if row and row[0] and not ok:
                con.execute("UPDATE devices SET greeted=NULL WHERE k=?", (k,))  # been away: greet again
            con.execute("INSERT INTO devices(k, seen) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET seen=?",
                        (k, now, now))
        con.close()
        return ok
    except sqlite3.Error:
        return False


def set_greeted(k):
    now = time.time()
    try:
        con = _db()
        with con:
            con.execute("INSERT INTO devices(k, greeted, seen) VALUES(?, ?, ?) "
                        "ON CONFLICT(k) DO UPDATE SET greeted=?, seen=?", (k, now, now, now, now))
        con.close()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- pages
def page(done=False, android=False):
    a = html.escape(ADDRESS)
    alt = f'<p class="small">or <b>{html.escape(IP)}</b></p>'
    # Offline, Android's HTTPS check always fails, so its sign-in window can't close by itself.
    tip = ('<p class="small tip"><b>Android:</b> if this window stays open, tap <b>&#8942;</b> '
           '(top right) &rarr; <b>Use this network as is</b>.</p>') if android else ""
    if done:
        head = "You're all set"
        body = (f'<p>Now open your web browser (Chrome, Safari…) and go to:</p>'
                f'<div class="addr" id="a">{a}</div>' + alt +
                f'<button class="btn" id="copy" type="button">Copy address</button>'
                f'<p class="small">You can close this window.</p>' + tip)
    else:
        head = f"Connected to {html.escape(NAME)}"
        body = (f'<p>This window can\'t show the server properly.<br>'
                f'Tap <b>OK</b>, then open your web browser (Chrome, Safari…) and go to:</p>'
                f'<div class="addr" id="a">{a}</div>' + alt +
                f'<button class="btn" id="copy" type="button">Copy address</button>'
                f'<a class="btn ok" href="/portal/ok">OK</a>' + tip)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(NAME)}</title>
<style>
:root{{color-scheme:dark}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0e110f;color:#e8ece6;
font:17px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:24px}}
main{{width:100%;max-width:420px;border:1px solid #2c342e;background:#151a16;padding:26px 22px;text-align:center}}
.chk{{width:56px;height:56px;margin:0 auto 10px;border:2px solid #8fd46b;color:#8fd46b;display:flex;align-items:center;justify-content:center;font-size:30px}}
h1{{font-size:22px;margin:0 0 2px}}
.sub{{margin:0 0 18px;color:#9aa596;font-size:13px;letter-spacing:.14em;text-transform:uppercase}}
p{{margin:14px 0}}
.addr{{font:600 28px/1.2 ui-monospace,Menlo,Consolas,monospace;color:#8fd46b;border:1px dashed #3d4a3f;padding:12px;margin:8px 0 14px;user-select:all;-webkit-user-select:all}}
.btn{{display:block;width:100%;min-height:50px;margin:10px 0 0;font:600 17px system-ui,sans-serif;border:1px solid #3d4a3f;background:#1d241f;color:#e8ece6;text-decoration:none;line-height:1.3;padding:8px 12px;display:flex;align-items:center;justify-content:center;cursor:pointer}}
.btn.ok{{background:#8fd46b;border-color:#8fd46b;color:#0e110f}}
.small{{font-size:14px;color:#9aa596}}
.tip{{margin-top:18px;border-top:1px solid #2c342e;padding-top:12px}}
</style></head>
<body><main>
<div class="chk" aria-hidden="true">✓</div>
<h1>{head}</h1>
<p class="sub">{html.escape(SUBTITLE)}</p>
{body}
</main>
<script>
(function(){{
  var b=document.getElementById('copy'), t='http://{a}';
  b.onclick=function(){{
    var ok=false;
    try{{var i=document.createElement('textarea');i.value=t;i.setAttribute('readonly','');i.style.position='fixed';i.style.opacity='0';
      document.body.appendChild(i);i.select();ok=document.execCommand('copy');document.body.removeChild(i);}}catch(e){{}}
    if(!ok&&navigator.clipboard){{navigator.clipboard.writeText(t).then(function(){{}},function(){{}});ok=true;}}
    b.textContent=ok?'Copied ✓':'Type {a} in your browser';
    try{{var x=new XMLHttpRequest();x.open('GET','/portal/ok?quiet=1');x.send();}}catch(e){{}}
  }};
}})();
</script>
</body></html>""".encode("utf-8")


def respond(start_response, status, body=b"", ctype="text/html; charset=utf-8", extra=()):
    headers = [("Content-Type", ctype), ("Content-Length", str(len(body))),
               ("Cache-Control", "no-store, no-cache, must-revalidate"), ("Pragma", "no-cache")]
    headers.extend(extra)
    start_response(status, headers)
    return [body]


# ---------------------------------------------------------------- app
def application(environ, start_response):
    host = (environ.get("HTTP_HOST") or "").split(":")[0].lower()
    path = environ.get("PATH_INFO") or "/"
    k = _key(environ)
    android = "android" in (environ.get("HTTP_USER_AGENT") or "").lower()

    if path == "/favicon.ico":
        return respond(start_response, "404 Not Found", b"")
    if path in ("/portal", "/portal/"):
        return respond(start_response, "200 OK", page(android=android))
    if path == "/portal/ok":
        set_greeted(k)
        if "quiet=1" in (environ.get("QUERY_STRING") or ""):
            return respond(start_response, "204 No Content")
        return respond(start_response, "200 OK", page(done=True, android=android))

    greeted = is_greeted(k)

    # Apple: the probe answer itself is shown in the sign-in sheet
    if host in APPLE_HOSTS or host.endswith(".apple.com.edgekey.net"):
        if greeted:
            return respond(start_response, "200 OK", APPLE_SUCCESS)
        return respond(start_response, "200 OK", page())

    if greeted:
        # the "online" answers each system expects
        if host.endswith("msftconnecttest.com"):
            return respond(start_response, "200 OK", b"Microsoft Connect Test", "text/plain")
        if "msftncsi.com" in host:
            return respond(start_response, "200 OK", b"Microsoft NCSI", "text/plain")
        if host == "detectportal.firefox.com":
            return respond(start_response, "200 OK", b"success\n", "text/plain")
        return respond(start_response, "204 No Content")      # Android, ChromeOS, Linux

    # not yet greeted: send the sign-in window to the message
    return respond(start_response, "302 Found", b"",
                   extra=[("Location", "http://%s/portal" % (host or "captive.lan"))])


if __name__ == "__main__":       # local test: python3 capture-wsgi.py [port]
    import sys
    from wsgiref.simple_server import make_server
    make_server("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 9090, application).serve_forever()
