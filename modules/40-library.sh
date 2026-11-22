# shellcheck shell=bash
# Library: kiwix-serve for ZIM files (Wikipedia, WikiMed, survival...). Installed empty; add ZIMs later.

say "Library (Kiwix)"
command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
apt_install kiwix-tools
ok "kiwix-tools $(dpkg-query -W -f '${Version}' kiwix-tools)"

export ZIM_DIR="$CONTENT_DIR/zims"
content_folder "$ZIM_DIR"
install -d -m 755 /var/lib/kiwix
E="$(mktemp)"; printf 'ZIM_DIR=%s\nLIBRARY_XML=%s\n' "$ZIM_DIR" /var/lib/kiwix/library.xml > "$E"
install_if_changed "$E" "$BOX_ETC/library.env" || true; rm -f "$E"
install_if_changed "$HERE/files/library/box-kiwix-library" /usr/local/sbin/box-kiwix-library 755 || true
ok "$(/usr/local/sbin/box-kiwix-library)"

changed=no
for u in kiwix-serve.service box-kiwix-library.service box-kiwix-library.path; do
  T="$(mktemp)"; render "$HERE/files/library/$u" "$T"
  install_if_changed "$T" "/etc/systemd/system/$u" && changed=yes; rm -f "$T"
done
[ "$changed" = yes ] && systemctl daemon-reload
systemctl enable -q kiwix-serve
systemctl enable -q --now box-kiwix-library.path
if [ "$changed" = yes ] || ! systemctl is-active --quiet kiwix-serve; then systemctl restart kiwix-serve; sleep 2; fi
systemctl is-active --quiet kiwix-serve || { journalctl -u kiwix-serve -n 20 --no-pager; die "kiwix-serve didn't start"; }
ok "kiwix-serve on 127.0.0.1:8184 (/kiwix); new ZIMs in $ZIM_DIR show up by themselves"
nginx_snippet "$HERE/files/nginx/kiwix-nginx.conf" kiwix.conf

say "Checks"
c="$(http_code '/kiwix/catalog/v2/entries?count=1')"
[ "$c" = 200 ] && ok "/kiwix/catalog answers (the Library page reads it)" || die "/kiwix/catalog answered $c"
n=$(find "$ZIM_DIR" -maxdepth 1 -name '*.zim' ! -name '.*' | wc -l)
[ "$n" -gt 0 ] && ok "$n ZIM file(s) in $ZIM_DIR" || info "no ZIM files yet: the Library card shows \"Nothing added yet\""
part_installed library
doctor_check library "service kiwix-serve Library" "http /kiwix/catalog/v2/entries?count=1 kiwix-serve Library catalog"
