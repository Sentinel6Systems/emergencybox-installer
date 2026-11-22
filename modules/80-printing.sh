# shellcheck shell=bash
# Printing: CUPS shares driverless printers (USB or network) with phones on the hotspot.

say "Printing (CUPS)"
apt_install cups cups-ipp-utils avahi-daemon ipp-usb
# cups-browsed would add its own copy of every printer it sees; box-printers does that job
systemctl disable --now cups-browsed >/dev/null 2>&1 || true
ok "cups $(dpkg-query -W -f='${Version}' cups), avahi, ipp-usb (USB printers without drivers)"

changed=no
C=/etc/cups/cupsd.conf
[ -f "$C.before-box" ] || cp -p "$C" "$C.before-box"
if ! cmp -s "$HERE/files/printing/cupsd.conf" "$C"; then
  install -m 640 -o root -g lp "$HERE/files/printing/cupsd.conf" "$C"
  L="$(mktemp)"
  cupsd -t -c "$C" >"$L" 2>&1 || { cat "$L"; cp -p "$C.before-box" "$C"; die "CUPS rejected the settings; put the original back"; }
  rm -f "$L"; changed=yes
  ok "cupsd.conf: shares printers with local networks only, no web admin (original kept)"
else
  ok "cupsd.conf already set"
fi

firewall_ports 80-printing "tcp 631" "udp 5353"
ok "firewall: hotspot can reach printing (631) and printer discovery (5353)"

systemctl enable -q cups avahi-daemon
if [ "$changed" = yes ] || ! systemctl is-active --quiet cups; then systemctl restart cups; fi
systemctl is-active --quiet avahi-daemon || systemctl restart avahi-daemon
install_if_changed "$HERE/files/printing/box-printers" /usr/local/sbin/box-printers 755 || true
u=no
for f in box-printers.service box-printers.timer; do install_if_changed "$HERE/files/printing/$f" "/etc/systemd/system/$f" && u=yes; done
[ "$u" = yes ] && systemctl daemon-reload
systemctl enable -q --now box-printers.timer
ok "box-printers: finds driverless printers 1 minute after boot, then every 2 minutes"

say "Checks"
for _ in $(seq 15); do lpstat -r >/dev/null 2>&1 && break; sleep 1; done
lpstat -r >/dev/null 2>&1 && ok "CUPS is running" || die "CUPS isn't answering (journalctl -u cups -n 30)"
out="$(/usr/local/sbin/box-printers 2>&1)"; [ -n "$out" ] && echo "$out" | sed 's/^/         /'
# lpstat -v = real queues only (lpstat -e also lists printers merely seen on the network)
n=$(lpstat -v 2>/dev/null | grep -c '^device for ' || true)
[ "$n" -gt 0 ] && ok "$n printer queue(s): $(lpstat -v | sed -n 's/^device for \([^:]*\):.*/\1/p' | paste -sd' ' -)" || info "no printers yet: plug one in (USB) or put one on the network; it's shared within 2 minutes"
part_installed printing
doctor_check printing "service cups Printing" "service avahi-daemon Printer discovery"
