# shellcheck shell=bash
# Landing page, Library and Books pages, and boxd (stats, devices, parts, restart, turn off).

WEB="$WWW_DIR/box"
command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
BACKUPS=/var/lib/emergencybox/page-backups

say "Landing page"
NEW="$(mktemp -d)"
render_tree "$HERE/files/www/box" "$NEW"
if [ -d "$WEB" ] && diff -rq "$NEW" "$WEB" >/dev/null 2>&1; then
  ok "pages in $WEB already up to date"
else
  if [ -f "$WEB/assets/box.js" ]; then        # a real landing page is there (not the placeholder)
    install -d -m 700 "$BACKUPS"
    BK="$BACKUPS/box-www-$(date +%F-%H%M%S).tar.gz"
    tar czf "$BK" -C "$(dirname "$WEB")" "$(basename "$WEB")"
    ls -1t "$BACKUPS"/box-www-*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm -f   # keep the last 5
    ok "backup of the previous pages: $BK"
  fi
  rm -rf "$WEB.new"; cp -a "$NEW" "$WEB.new"
  chmod -R u=rwX,go=rX "$WEB.new"
  rm -rf "$WEB.old"; [ -d "$WEB" ] && mv "$WEB" "$WEB.old"
  mv "$WEB.new" "$WEB"; rm -rf "$WEB.old"
  ok "pages installed in $WEB (maps open at $MAP_LAT, $MAP_LON, zoom $MAP_ZOOM)"
fi
rm -rf "$NEW"

say "boxd (live stats, connected devices, parts, restart and turn off)"
changed=no
install_if_changed "$HERE/files/boxd/boxd.py" /opt/boxd/boxd.py 755 && changed=yes
export HOTSPOT_IF="${HOTSPOT_IF:-$(. "$BOX_ETC/network.env" 2>/dev/null; echo "${HOTSPOT_IF:-wlan0}")}"
T="$(mktemp)"; render "$HERE/files/boxd/boxd.service" "$T"
install_if_changed "$T" /etc/systemd/system/boxd.service && { changed=yes; systemctl daemon-reload; }; rm -f "$T"
systemctl enable -q boxd
if [ "$changed" = yes ] || ! systemctl is-active --quiet boxd; then systemctl restart boxd; sleep 2; fi
systemctl is-active --quiet boxd && ok "boxd running on 127.0.0.1:8181 (hotspot $HOTSPOT_IF)" || die "boxd didn't start: journalctl -u boxd -n 30"
nginx_snippet "$HERE/files/nginx/boxd-nginx.conf" boxd.conf

say "Checks"
[ "$(http_code /box/)" = 200 ] && ok "/box/ landing page" || die "/box/ is not answering 200"
grep -q 'id="grid"' <(curl -s -H "Host: $ADDRESS" http://127.0.0.1/box/) && ok "/box/ is the landing page, not the placeholder" || die "/box/ still shows the placeholder"
[ "$(http_code /api/stats)" = 200 ] && ok "/api/stats" || die "/api/stats is not answering 200"
curl -s -H "Host: $ADDRESS" http://127.0.0.1/api/parts | grep -q '"parts"' && ok "/api/parts" || die "/api/parts is not answering"
doctor_check landing "service boxd Landing page helper" "http /api/stats boxd Live stats"
