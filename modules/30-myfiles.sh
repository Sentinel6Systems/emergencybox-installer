# shellcheck shell=bash
# My files: the family's own documents (a plain folder with a size limit), plus USB drives.

command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
say "My files ($MYFILES_DIR)"
content_folder "$MYFILES_DIR" root
lim="up to $MYFILES_MAX_GB GB"; [ "$MYFILES_MAX_GB" = 0 ] && lim="no own limit"
ok "folder ready ($lim; uploads stop $SPACE_RESERVE_GB GB before the disk is full)"

say "USB drives: FAT32, exFAT and NTFS at /media/usbN"
apt_install exfatprogs ntfs-3g dosfstools
changed=no
install_if_changed "$HERE/files/usb/box-usb" /usr/local/sbin/box-usb 755 && changed=yes
install_if_changed "$HERE/files/usb/box-usb@.service" /etc/systemd/system/box-usb@.service && changed=yes
install_if_changed "$HERE/files/usb/90-emergencybox-usb.rules" /etc/udev/rules.d/90-emergencybox-usb.rules && changed=yes
if [ "$changed" = yes ]; then
  systemctl daemon-reload
  udevadm control --reload
  udevadm trigger --action=add --subsystem-match=block
  udevadm settle
  # udev only queues the mount services; give them a moment to finish before counting
  for _ in $(seq 15); do
    systemctl list-units 'box-usb@*' --state=activating --no-legend 2>/dev/null | grep -q . || break
    sleep 1
  done
fi
n=$(findmnt -rno TARGET | grep -c '^/media/usb[0-9]' || true)
ok "automount on (udev + box-usb@.service); drives mounted now: $n"
findmnt -rno TARGET,SOURCE,FSTYPE | grep '^/media/usb' | sed 's/^/         /' || true
info "Plug drives in while the box is off, or into a powered hub: hot-plugging next to a USB SSD can crash a Pi 4"

say "boxfiles service"
changed=no
install_if_changed "$HERE/files/boxfiles/boxfiles.py" /opt/boxfiles/boxfiles.py 755 && changed=yes
T="$(mktemp)"; render "$HERE/files/boxfiles/boxfiles.service" "$T"
install_if_changed "$T" /etc/systemd/system/boxfiles.service && { changed=yes; systemctl daemon-reload; }; rm -f "$T"
systemctl enable -q boxfiles
if [ "$changed" = yes ] || ! systemctl is-active --quiet boxfiles; then systemctl restart boxfiles; sleep 1; fi
systemctl is-active --quiet boxfiles && ok "running on 127.0.0.1:8182" || die "boxfiles didn't start: journalctl -u boxfiles -n 30"
nginx_snippet "$HERE/files/nginx/boxfiles-nginx.conf" boxfiles.conf

say "Checks"
roots=$(curl -s --max-time 10 -H "Host: $ADDRESS" http://127.0.0.1/api/files/roots || true)
grep -q '"kind": "my"' <<< "$roots" && ok "/api/files/roots lists My files" || die "/api/files/roots: $roots"
grep -q '"missing"' <<< "$roots" && die "My files shows as missing: $roots"
T=$(mktemp); head -c 200000 /dev/urandom > "$T"; NAME="install-test-$$.bin"
up=$(curl -s --max-time 30 -H "Host: $ADDRESS" -X PUT --data-binary @"$T" "http://127.0.0.1/api/files/upload?root=my&path=&name=$NAME" || true)
if grep -q '"ok": true' <<< "$up"; then
  if curl -s --max-time 30 -H "Host: $ADDRESS" "http://127.0.0.1/api/files/get?root=my&path=$NAME&dl=1" | cmp -s - "$T"; then
    ok "upload + download round trip"
  else
    rm -f "$MYFILES_DIR/$NAME" "$T"; die "uploaded, but the download didn't match (check /_boxfiles_my/ in nginx)"
  fi
  curl -s --max-time 10 -H "Host: $ADDRESS" -X POST -d "{\"root\":\"my\",\"path\":\"$NAME\"}" http://127.0.0.1/api/files/delete >/dev/null || true
  rm -f "$MYFILES_DIR/$NAME"
else
  rm -f "$T"; die "test upload failed: $up"
fi
rm -f "$T"
part_installed files
doctor_check myfiles "service boxfiles My files service" "http /api/files/roots boxfiles My files"
