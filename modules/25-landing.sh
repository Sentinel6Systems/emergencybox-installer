# shellcheck shell=bash
# Landing page, Library and Books pages, and boxd (stats, devices, parts, restart, turn off).

WEB="$WWW_DIR/box"
command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
BACKUPS=/var/lib/emergencybox/page-backups

say "Landing page"
NEW="$(mktemp -d)"
# box.css?v=<its checksum>: browsers fetch the new stylesheet after an update instead of a cached one
export CSS_VERSION="$(sha256sum "$HERE/files/www/box/assets/box.css" | cut -c1-10)"
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

say "Power source (PiSugar S Plus battery)"
# GPIO 3 reads low on wall power, high on battery. Without a PiSugar its pull-up reads high too,
# so the watch is on only when a PiSugar is set or found. I2C stays off: it would take the pin.
PCONF="$BOX_ETC/power.conf"
pisugar="$PISUGAR"
if [ "$pisugar" = auto ]; then
  if [ -f "$PCONF" ]; then pisugar="$(sed -n 's/^PISUGAR=//p' "$PCONF")"            # decided on an earlier run
  elif command -v pinctrl >/dev/null && pinctrl get 3 | grep -Eq '^ *3: *ip .*\| *lo'; then pisugar=yes
  else pisugar=no; fi
fi
if [ ! -f "$PCONF" ]; then
  cat > "$PCONF" <<EOF
# Read by boxd every few seconds: changes need no reinstall.
# PISUGAR=yes watches GPIO 3 (low = wall power, high = battery). Only with a PiSugar S Plus.
PISUGAR=$pisugar
# Clean shutdown after this many minutes on battery (0 = never). A full 5000 mAh battery runs a
# busy Pi 4 with an SSD about 2-3 hours; 90 leaves room for a battery that wasn't full.
# The PiSugar starts the box again when wall power comes back (its auto-start switch must be ON).
BATTERY_SHUTDOWN_MIN=90
EOF
  ok "power settings: $PCONF (PISUGAR=$pisugar, shutdown after 90 min on battery)"
elif ! grep -qx "PISUGAR=$pisugar" "$PCONF"; then
  sed -i "s/^PISUGAR=.*/PISUGAR=$pisugar/" "$PCONF"; ok "power settings: PISUGAR=$pisugar (from box.conf)"
else ok "power settings: $PCONF (unchanged)"; fi
if [ "$pisugar" = yes ]; then
  command -v pinctrl >/dev/null || die "PISUGAR=yes needs pinctrl (Raspberry Pi OS): sudo apt install raspi-utils-core"
  if grep -Eq '^[[:space:]]*dtparam=i2c_arm=on' /boot/firmware/config.txt 2>/dev/null; then
    warn "I2C is on in /boot/firmware/config.txt: it takes GPIO 3, so the power source shows as unknown"
  fi
fi

say "Checks"
[ "$(http_code /box/)" = 200 ] && ok "/box/ landing page" || die "/box/ is not answering 200"
grep -q 'id="grid"' <(curl -s -H "Host: $ADDRESS" http://127.0.0.1/box/) && ok "/box/ is the landing page, not the placeholder" || die "/box/ still shows the placeholder"
[ "$(http_code /api/stats)" = 200 ] && ok "/api/stats" || die "/api/stats is not answering 200"
curl -s -H "Host: $ADDRESS" http://127.0.0.1/api/parts | grep -q '"parts"' && ok "/api/parts" || die "/api/parts is not answering"
for _ in 1 2 3 4 5 6 7 8 9 10; do     # the source settles about 6 s after boxd starts
  src="$(curl -s -H "Host: $ADDRESS" http://127.0.0.1/api/power | python3 -c 'import json,sys; print(json.load(sys.stdin)["source"])' 2>/dev/null)"
  [ "$pisugar" = yes ] && [ "$src" = unknown ] || break; sleep 1
done
[ -n "$src" ] || die "/api/power is not answering"
if [ "$pisugar" = yes ] && [ "$src" = unknown ]; then warn "/api/power: power source unknown (can't read GPIO 3; is I2C on?)"
else ok "/api/power (power source: $src)"; fi
doctor_check landing "service boxd Landing page helper" "http /api/stats boxd Live stats"
