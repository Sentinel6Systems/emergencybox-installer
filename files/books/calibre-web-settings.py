#!/usr/bin/env python3
"""Apply the EmergencyBox settings to Calibre-Web's app.db (run while Calibre-Web is stopped).
argv: app.db  library_dir  box_name"""
import sqlite3, sys
db, libdir, title = sys.argv[1:4]
ROLE_DOWNLOAD, ROLE_VIEWER = 2, 256
c = sqlite3.connect(db)
cols = {r[1] for r in c.execute("PRAGMA table_info(settings)")}
want = {
    "config_calibre_dir": libdir,     # the Calibre library (metadata.db + books)
    "config_calibre_web_title": title,
    "config_anonbrowse": 1,           # the Books page reads the list without signing in
    "config_public_reg": 0,           # nobody can make an account
    "config_uploading": 1,            # the admin signs in to Calibre-Web to add books (Admin page links there)
    "config_password_policy": 0,      # allow the box's plain admin password
    "config_check_extensions": 1,
}
missing = [k for k in want if k not in cols]
if missing:     # a renamed setting in a new Calibre-Web version: stop rather than run half-configured
    sys.exit("Calibre-Web has no setting(s) %s; check versions.lock" % ", ".join(missing))
for k, v in want.items():
    c.execute(f"UPDATE settings SET {k}=?", (v,))
# Guest (anonymous) may view and download
c.execute("UPDATE user SET role = role | ? WHERE name='Guest'", (ROLE_DOWNLOAD | ROLE_VIEWER,))
c.commit()
print("settings applied:", ", ".join(k for k in want if k in cols))
