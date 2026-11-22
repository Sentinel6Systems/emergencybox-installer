# shellcheck shell=bash
# box-doctor: the self-check, at boot and every 15 minutes; restarts what's down, once.

say "Self-check (box-doctor)"
install_if_changed "$HERE/files/doctor/box-doctor" /usr/local/sbin/box-doctor 755 || true
u=no
for f in box-doctor.service box-doctor.timer; do install_if_changed "$HERE/files/doctor/$f" "/etc/systemd/system/$f" && u=yes; done
[ "$u" = yes ] && systemctl daemon-reload
systemctl enable -q --now box-doctor.timer
ok "box-doctor: 2 minutes after boot, then every 15 minutes"
info "checks listed in $BOX_ETC/doctor.d/: $(ls "$BOX_ETC/doctor.d/" 2>/dev/null | sed 's/\.check//' | paste -sd' ' -)"
if /usr/local/sbin/box-doctor | sed 's/^/         /'; then ok "all checks pass"; else die "box-doctor found problems (above)"; fi
