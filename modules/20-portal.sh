# shellcheck shell=bash
# Wi-Fi sign-in window: shows "connected, open your browser" instead of the landing page.

say "Captive portal message (boxportal)"
command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
STATE=/var/lib/boxportal
service_user boxportal "$STATE"
chown -R boxportal:boxportal "$STATE"     # files in its folder are always its own
changed=no
install -d /opt/emergencybox/portal
T="$(mktemp)"; render "$HERE/files/portal/capture-wsgi.py" "$T"
install_if_changed "$T" /opt/emergencybox/portal/capture-wsgi.py && changed=yes; rm -f "$T"
install_if_changed "$HERE/files/portal/boxportal-server.py" /opt/emergencybox/portal/boxportal-server.py && changed=yes
T="$(mktemp)"; render "$HERE/files/portal/boxportal.service" "$T"
install_if_changed "$T" /etc/systemd/system/boxportal.service && { changed=yes; systemctl daemon-reload; }; rm -f "$T"
systemctl enable -q boxportal
if [ "$changed" = yes ] || ! systemctl is-active --quiet boxportal; then systemctl restart boxportal; sleep 1; fi
systemctl is-active --quiet boxportal || { journalctl -u boxportal -n 20 --no-pager; die "boxportal didn't start"; }
ok "boxportal running ($BOX_NAME, $ADDRESS or $HOTSPOT_IP); state in $STATE"

say "Checks"
pcode() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Host: $1" "http://127.0.0.1$2"; }
pbody() { curl -s --max-time 5 -H "Host: $1" "http://127.0.0.1$2"; }
# Runs as boxportal and only on an existing file: sqlite3 as root would create a root-owned
# database the service can't write.
forget_me() {
  local db="$STATE/boxportal.sqlite"
  [ -f "$db" ] && runuser -u boxportal -- sqlite3 "$db" "DELETE FROM devices WHERE k='ip:127.0.0.1'" 2>/dev/null || true
}
forget_me
[ "$(pcode connectivitycheck.gstatic.com /generate_204)" = 302 ] && ok "Android check -> message" || die "Android check did not redirect to the message"
pbody captive.apple.com /hotspot-detect.html | grep -q "Connected to $BOX_NAME" && ok "iPhone check -> message" || die "iPhone check did not show the message"
pbody connectivitycheck.gstatic.com /portal/ok | grep -q "all set" && ok "OK button" || die "OK button page missing"
[ "$(pcode connectivitycheck.gstatic.com /generate_204)" = 204 ] && ok "after OK: Android check -> online (204)" || die "after OK: Android check not 204"
[ "$(http_code /box/)" = 200 ] && ok "$ADDRESS/box/ still the box's page" || die "$ADDRESS/box/ broken"
forget_me
doctor_check portal "service boxportal Wi-Fi sign-in message"
