# shellcheck shell=bash
# Books: Calibre-Web with an empty Calibre library. Read online, download, OPDS for the Books page.

say "Books (Calibre-Web)"
command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
. "$HERE/versions.lock"
apt_install python3-venv python3-dev build-essential libxml2-dev libxslt1-dev zlib1g-dev libjpeg-dev
CONF=/var/lib/calibre-web; APPDB="$CONF/app.db"
export CALIBRE_LIB="$CONTENT_DIR/books"
# Calibre-Web insists on making ~/.calibre-web, so its home is its settings folder
service_user calibreweb "$CONF" content
[ "$(getent passwd calibreweb | cut -d: -f6)" = "$CONF" ] || { systemctl stop calibre-web 2>/dev/null || true; usermod -d "$CONF" calibreweb; }
# It writes new books and metadata.db itself, so the library folder is its own (group: content)
content_folder "$CALIBRE_LIB" calibreweb

V=/opt/calibre-web/venv
PIPLOG=/var/log/emergencybox-pip.log
if [ "$("$V/bin/pip" show calibreweb 2>/dev/null | sed -n 's/^Version: //p')" != "$CALIBREWEB_VERSION" ]; then
  python3 -m venv "$V"
  "$V/bin/pip" install -q --upgrade pip wheel >"$PIPLOG" 2>&1 || { tail -20 "$PIPLOG"; die "pip upgrade failed"; }
  info "installing Calibre-Web $CALIBREWEB_VERSION (a few minutes on a Pi)..."
  "$V/bin/pip" install -q "calibreweb==$CALIBREWEB_VERSION" >>"$PIPLOG" 2>&1 \
    || { tail -30 "$PIPLOG"; die "Calibre-Web didn't install (log: $PIPLOG)"; }
fi
ok "Calibre-Web $CALIBREWEB_VERSION in $V (Python $("$V/bin/python" -V | cut -d' ' -f2))"

# An empty Calibre library, if there isn't one yet
if [ ! -f "$CALIBRE_LIB/metadata.db" ]; then
  T="$(mktemp -d)"
  python3 - "$HERE/files/books/empty-metadata.sql" "$T/metadata.db" <<'PY'
import sqlite3, sys, uuid
sql = open(sys.argv[1]).read().replace("00000000-0000-0000-0000-000000000000", str(uuid.uuid4()))
c = sqlite3.connect(sys.argv[2]); c.executescript(sql); c.commit(); c.close()
PY
  install -o calibreweb -g "$CONTENT_GROUP" -m 664 "$T/metadata.db" "$CALIBRE_LIB/metadata.db"; rm -rf "$T"
  ok "empty Calibre library created in $CALIBRE_LIB (its own library id)"
else
  ok "Calibre library: $CALIBRE_LIB/metadata.db"
fi

changed=no
T="$(mktemp)"; render "$HERE/files/books/calibre-web.service" "$T"
install_if_changed "$T" /etc/systemd/system/calibre-web.service && { changed=yes; systemctl daemon-reload; }; rm -f "$T"
install_if_changed "$HERE/files/books/calibre-web-settings.py" /opt/calibre-web/calibre-web-settings.py 755 && changed=yes
systemctl enable -q calibre-web

# First start makes app.db; then the box's settings go in while it's stopped. Only when
# something they depend on changed, so a re-run doesn't restart Calibre-Web.
if [ ! -f "$APPDB" ]; then
  systemctl start calibre-web
  for _ in $(seq 60); do [ -f "$APPDB" ] && curl -s -o /dev/null http://127.0.0.1:8083/ && break; sleep 2; done
  [ -f "$APPDB" ] || { journalctl -u calibre-web -n 30 --no-pager; die "Calibre-Web didn't create its settings"; }
fi
stamp="$(printf '%s\n' "$CALIBREWEB_VERSION" "$CALIBRE_LIB" "$BOX_NAME" "$ADMIN_PASSWORD" | cat - /opt/calibre-web/calibre-web-settings.py | sha256sum | cut -d' ' -f1)"
if [ "$stamp" != "$(cat "$BOX_ETC/books.sha256" 2>/dev/null)" ]; then
  systemctl stop calibre-web
  runuser -u calibreweb -- env HOME="$CONF" "$V/bin/python" /opt/calibre-web/calibre-web-settings.py "$APPDB" "$CALIBRE_LIB" "$BOX_NAME" | sed 's/^/         /'
  runuser -u calibreweb -- env HOME="$CONF" CALIBRE_DBPATH="$CONF" "$V/bin/cps" -p "$APPDB" -s "admin:$ADMIN_PASSWORD" >/dev/null 2>&1 \
    && ok "Calibre-Web admin login: admin / (ADMIN_PASSWORD)" || die "couldn't set the Calibre-Web admin password"
  echo "$stamp" > "$BOX_ETC/books.sha256"
  changed=yes
else
  ok "settings and admin login already applied"
fi
chown -R calibreweb:calibreweb "$CONF"
if [ "$changed" = yes ] || ! systemctl is-active --quiet calibre-web; then
  systemctl restart calibre-web
  for _ in $(seq 60); do curl -s -o /dev/null http://127.0.0.1:8083/ && break; sleep 2; done
fi
systemctl is-active --quiet calibre-web || { journalctl -u calibre-web -n 30 --no-pager; die "Calibre-Web didn't start"; }
ok "Calibre-Web on 127.0.0.1:8083 (/books)"
nginx_snippet "$HERE/files/nginx/calibre-web-nginx.conf" calibre-web.conf

say "Checks"
c="$(http_code /books/opds/new)"
[ "$c" = 200 ] && ok "/books/opds/new answers without signing in (the Books page reads it)" || die "/books/opds/new answered $c"
part_installed books
doctor_check books "service calibre-web Books" "http /books/opds/new calibre-web Books list"
