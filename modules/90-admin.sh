# shellcheck shell=bash
# Admin page: Wi-Fi, passwords, adding content, backups to USB, behind the admin password.

say "Admin page"
command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
[ -f "$WWW_DIR/box/index.html" ] || die "needs the landing module first (sudo ./install.sh landing)"

changed=no
install_if_changed "$HERE/files/boxadmin/boxadmin.py" /opt/boxadmin/boxadmin.py 755 && changed=yes
ok "boxadmin in /opt/boxadmin"

# The admin password: box.conf is the source; the service keeps only a salted hash
PASS="$BOX_ETC/admin.pass"
if ! python3 -c 'import sys; sys.path.insert(0, "/opt/boxadmin"); import boxadmin as b; b.PASSFILE = sys.argv[1]; sys.exit(0 if b.pw_ok(sys.argv[2]) else 1)' "$PASS" "$ADMIN_PASSWORD"; then
  ( umask 077
    python3 -c 'import sys; sys.path.insert(0, "/opt/boxadmin"); import boxadmin as b; print(b.hash_pw(sys.argv[1]))' "$ADMIN_PASSWORD" > "$PASS.tmp" )
  chmod 600 "$PASS.tmp"; mv -f "$PASS.tmp" "$PASS"
  ok "admin password set from box.conf (ADMIN_PASSWORD)"
else
  ok "admin password unchanged"
fi

T="$(mktemp)"; render "$HERE/files/boxadmin/boxadmin.service" "$T"
install_if_changed "$T" /etc/systemd/system/boxadmin.service && { changed=yes; systemctl daemon-reload; }; rm -f "$T"
systemctl enable -q boxadmin
if [ "$changed" = yes ] || ! systemctl is-active --quiet boxadmin; then systemctl restart boxadmin; fi
for _ in $(seq 20); do curl -s -o /dev/null http://127.0.0.1:8185/api/admin/state && break; sleep 1; done
systemctl is-active --quiet boxadmin || { journalctl -u boxadmin -n 30 --no-pager; die "boxadmin didn't start"; }
ok "boxadmin on 127.0.0.1:8185"
nginx_snippet "$HERE/files/nginx/boxadmin-nginx.conf" boxadmin.conf

say "Checks"
c="$(http_code /box/admin/)"; [ "$c" = 200 ] && ok "/box/admin/ page" || die "/box/admin/ answered $c (run: sudo ./install.sh landing)"
s="$(curl -s -H "Host: $ADDRESS" http://127.0.0.1/api/admin/state)"
[ "$s" = '{"signed_in": false}' ] && ok "/api/admin/state: not signed in without the password" || die "/api/admin/state answered: $s"
c="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $ADDRESS" -H 'X-Box-Admin: 1' -H 'Content-Type: application/json' \
      --data '{"password":"wrong-password"}' http://127.0.0.1/api/admin/login)"
[ "$c" = 401 ] && ok "a wrong password is refused" || die "wrong password answered $c"
J="$(mktemp)"
python3 -c 'import json,sys; print(json.dumps({"password": sys.argv[1]}))' "$ADMIN_PASSWORD" > "$J"
c="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $ADDRESS" -H 'X-Box-Admin: 1' -H 'Content-Type: application/json' \
      --data @"$J" http://127.0.0.1/api/admin/login)"
rm -f "$J"
[ "$c" = 200 ] && ok "the admin password signs in" || die "sign-in with ADMIN_PASSWORD answered $c"
part_installed admin
doctor_check admin "service boxadmin Admin page" "http /api/admin/state boxadmin Admin page API"
