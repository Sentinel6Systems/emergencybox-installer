# shellcheck shell=bash
# Web front: nginx with the box's site (box.lan, the captive checks); parts add their pages to it.

say "Web front (nginx)"
apt_install nginx
ok "nginx $(nginx -v 2>&1 | sed 's|.*/||')"

install -d /etc/nginx/emergencybox.d "$WWW_DIR/box"
# Placeholder home page until the landing module puts the real one there
[ -f "$WWW_DIR/box/index.html" ] || render "$HERE/files/web/placeholder/box/index.html" "$WWW_DIR/box/index.html"

CAPTIVE_HOSTS="$(grep -vE '^[[:space:]]*(#|$)' "$HERE/files/network/captive-hosts.list" | paste -sd' ' -)"
export CAPTIVE_HOSTS
SITE=/etc/nginx/sites-available/emergencybox
NEW="$(mktemp)"; render "$HERE/files/web/emergencybox-site.conf" "$NEW"
changed=no; wrote=no; bak=""
if ! cmp -s "$NEW" "$SITE"; then
  [ -f "$SITE" ] && { bak="$(mktemp)"; cp -p "$SITE" "$bak"; }
  install -m 644 "$NEW" "$SITE"; changed=yes; wrote=yes
fi
rm -f "$NEW"
ln -sfn "$SITE" /etc/nginx/sites-enabled/emergencybox
[ -e /etc/nginx/sites-enabled/default ] && { rm -f /etc/nginx/sites-enabled/default; ok "removed nginx's default site"; changed=yes; }

# nginx starts after the hotspot address exists, and restarts if it dies
U="$(mktemp)"
printf '[Unit]\nAfter=network-online.target NetworkManager.service\n[Service]\nRestart=on-failure\nRestartSec=3\n' > "$U"
install_if_changed "$U" /etc/systemd/system/nginx.service.d/emergencybox.conf && systemctl daemon-reload
rm -f "$U"

LOGT="$(mktemp)"
if nginx -t 2>"$LOGT"; then
  systemctl enable -q nginx
  if [ "$changed" = yes ] || ! systemctl is-active --quiet nginx; then systemctl reload-or-restart nginx; sleep 1; fi
  ok "site: $ADDRESS and $HOTSPOT_IP (pages), $(wc -w <<< "$CAPTIVE_HOSTS") captive-check names (portal)"
else
  cat "$LOGT"
  # Undo only what this run wrote; an unchanged site isn't the problem (see the message above)
  [ "$wrote" = no ] && die "nginx rejects its settings (above); the box's site wasn't changed"
  if [ -n "$bak" ]; then cp -p "$bak" "$SITE"; else rm -f "$SITE" /etc/nginx/sites-enabled/emergencybox; fi
  die "nginx rejected the site; put back what was there before"
fi
rm -f "$LOGT" "$bak"

c="$(http_code /)"; [ "$c" = 302 ] || die "/ answered $c, expected a redirect to /box/"
c="$(http_code /box/)"; [ "$c" = 200 ] || die "/box/ answered $c"
ok "http://$ADDRESS/ -> /box/ (200)"
doctor_check web "service nginx Web server" "http /box/ nginx Home page"
