# shellcheck shell=bash
# Base system: checks the machine, box folders and group, quiet disk, watchdog, Pi boot settings.

say "Base system"
. /etc/os-release
ok "$PRETTY_NAME, $(uname -m), $(ram_mb) MB RAM"
case "$(uname -m)" in aarch64|x86_64) ;; *) die "Needs a 64-bit system (arm64 or amd64); this is $(uname -m)" ;; esac
case "${ID:-} ${ID_LIKE:-}" in *debian*|*ubuntu*) ;; *) die "Needs Debian, Raspberry Pi OS or Ubuntu" ;; esac
[ "$(ram_mb)" -ge 1800 ] || warn "Under 2 GB RAM: maps and Jellyfin together will be tight"
fg="$(free_gb /)"
[ "$fg" -ge 32 ] || warn "Only ${fg} GB free on the disk: fine for the services, little room for content"
ok "free space: ${fg} GB"
online || info "no internet: fine for a re-run; a part that has to download something says so"

apt_install git curl rsync python3 sqlite3 ntfs-3g exfatprogs psmisc
ok "tools: git curl rsync python3 sqlite3 ntfs-3g exfatprogs psmisc"

# --- 1. Hardware profile ---------------------------------------------------------------
PROFILE="$(detect_profile)"
install -d "$BOX_ETC"
[ "$(cat "$BOX_ETC/profile" 2>/dev/null)" = "$PROFILE" ] || echo "$PROFILE" > "$BOX_ETC/profile"
ok "hardware profile: $PROFILE ($( { tr -d '\0' < /proc/device-tree/model; } 2>/dev/null || cat /sys/class/dmi/id/product_name 2>/dev/null || echo unknown))"

# --- 2. Name and clock --------------------------------------------------------------------
if [ "$(hostname)" != "$HOSTNAME" ]; then
  old="$(hostname)"
  hostnamectl set-hostname "$HOSTNAME"
  sed -i -E "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t$HOSTNAME/" /etc/hosts
  grep -q "^127\.0\.1\.1" /etc/hosts || printf '127.0.1.1\t%s\n' "$HOSTNAME" >> /etc/hosts
  ok "machine name: $old -> $HOSTNAME"
else
  ok "machine name: $HOSTNAME"
fi
if [ -n "$TIMEZONE" ]; then
  if [ "$(timedatectl show -p Timezone --value 2>/dev/null)" != "$TIMEZONE" ]; then
    timedatectl set-timezone "$TIMEZONE" && ok "time zone set to $TIMEZONE"
  else ok "time zone: $TIMEZONE"; fi
fi

# --- 3. Box folders: one disk, plain folders, one content group ------------------------------
# The owner copies content in without sudo; every service that reads content is in the group.
getent group "$CONTENT_GROUP" >/dev/null || groupadd --system "$CONTENT_GROUP"
id -nG "$OWNER" | tr ' ' '\n' | grep -qx "$CONTENT_GROUP" || { usermod -a -G "$CONTENT_GROUP" "$OWNER"; info "$OWNER joined $CONTENT_GROUP (takes effect at the next sign-in)"; }
install -d -m 755 "$BOX_ROOT"
content_folder "$CONTENT_DIR"
install -d "$BOX_ETC/parts" "$BOX_ETC/doctor.d" "$BOX_ETC/firewall.d" /var/lib/emergencybox
ok "folders: $CONTENT_DIR ($OWNER:$CONTENT_GROUP, 2775), settings in $BOX_ETC"
# Settings the box's own tools read (box-doctor, boxadmin); written only when they change
E="$(mktemp)"
printf 'ADDRESS=%s\nHOTSPOT_IP=%s\nCONTENT_DIR=%s\nMYFILES_DIR=%s\nSPACE_RESERVE_GB=%s\n' \
  "$ADDRESS" "$HOTSPOT_IP" "$CONTENT_DIR" "$MYFILES_DIR" "$SPACE_RESERVE_GB" > "$E"
install_if_changed "$E" "$BOX_ETC/box.env" || true; rm -f "$E"

# --- 4. Fewer disk writes, nothing that changes the box on its own ----------------------------
for t in plocate-updatedb.timer man-db.timer apt-daily.timer apt-daily-upgrade.timer; do
  if systemctl list-unit-files "$t" --no-legend 2>/dev/null | grep -q .; then
    systemctl disable --now "$t" >/dev/null 2>&1 || true
    systemctl mask "$t" >/dev/null 2>&1
  fi
done
systemctl disable --now unattended-upgrades.service >/dev/null 2>&1 || true
ok "off: plocate, man-db and apt timers, unattended upgrades"

removed=""
for p in cloud-init needrestart; do
  dpkg -s "$p" >/dev/null 2>&1 && { DEBIAN_FRONTEND=noninteractive apt-get purge -y -q "$p" >/dev/null 2>&1 || die "couldn't remove $p"; removed+=" $p"; }
done
[ -d /etc/cloud ] && touch /etc/cloud/cloud-init.disabled
ok "removed:${removed:- nothing left to remove (cloud-init, needrestart)}"

J="$(mktemp)"
cat > "$J" <<'EOF'
# EmergencyBox: keep logs on disk (crash evidence survives a restart) but small.
[Journal]
Storage=persistent
SystemMaxUse=100M
SystemMaxFileSize=10M
MaxRetentionSec=3month
EOF
if install_if_changed "$J" /etc/systemd/journald.conf.d/emergencybox.conf; then systemctl restart systemd-journald; fi
rm -f "$J"
ok "journal: on disk, max 100 MB"

# --- 5. Hardware watchdog: a frozen box restarts itself --------------------------------------
W="$(mktemp)"
cat > "$W" <<'EOF'
# EmergencyBox: hardware watchdog. The Pi's chip allows at most 15 s; systemd uses the most it can.
[Manager]
RuntimeWatchdogSec=1min
RebootWatchdogSec=2min
EOF
if install_if_changed "$W" /etc/systemd/system.conf.d/emergencybox-watchdog.conf; then systemctl daemon-reexec; fi
rm -f "$W"
if [ -e /dev/watchdog0 ]; then
  ok "hardware watchdog on: $(cat /sys/class/watchdog/watchdog0/timeout 2>/dev/null || echo '?') s timeout"
else warn "no hardware watchdog device on this machine"; fi

# --- 6. Profile-specific ----------------------------------------------------------------------
REBOOT=""
case "$PROFILE" in
  pi4|pi5)
    if [ "$PI_POWER_OFF_ON_HALT" = yes ] && command -v rpi-eeprom-config >/dev/null; then
      cur="$(rpi-eeprom-config 2>/dev/null || true)"
      if grep -q '^POWER_OFF_ON_HALT=1' <<< "$cur" && grep -q '^WAKE_ON_GPIO=0' <<< "$cur"; then
        ok "Pi EEPROM: powers off fully on shutdown"
      else
        tmp="$(mktemp)"; grep -vE '^(POWER_OFF_ON_HALT|WAKE_ON_GPIO)=' <<< "$cur" > "$tmp" || true
        printf 'POWER_OFF_ON_HALT=1\nWAKE_ON_GPIO=0\n' >> "$tmp"
        rpi-eeprom-config --apply "$tmp" >/dev/null 2>&1 && { ok "Pi EEPROM: power off fully on shutdown"; REBOOT+=" eeprom"; } \
          || warn "Pi EEPROM: couldn't apply POWER_OFF_ON_HALT=1 (sudo rpi-eeprom-config --edit)"
        rm -f "$tmp"
      fi
    fi
    # Boot line: memory limits for Docker/Jellyfin, UAS quirk for known-bad USB bridges, no imager seed
    add=("cgroup_enable=memory"); del=("ds=nocloud*")
    for q in $PI_USB_QUIRKS; do add+=("usb-storage.quirks=$q"); done
    if cmdline_edit "${add[*]}" "${del[*]}"; then ok "boot line: already set"; else ok "boot line updated (old copy kept)"; REBOOT+=" cmdline"; fi
    if is_pi && command -v vcgencmd >/dev/null; then
      th="$(vcgencmd get_throttled | cut -d= -f2)"
      if (( th & 0x50000 )); then warn "POWER: under-voltage or throttling since boot ($th). Use the official power supply."
      else ok "power: $th (no under-voltage since boot)"; fi
    fi ;;
  x86-laptop)
    L="$(mktemp)"
    printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' > "$L"
    install_if_changed "$L" /etc/systemd/logind.conf.d/emergencybox.conf || true; rm -f "$L"
    systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1
    ok "laptop: lid ignored, sleep and suspend off" ;;
  x86-mini)
    systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1
    ok "mini PC: sleep and suspend off" ;;
esac

if [ -n "$REBOOT" ]; then
  echo "${REBOOT# }" > "$BOX_ETC/reboot-needed"
  warn "restart needed for:$REBOOT (the install carries on; restart at the end)"
elif [ -f "$BOX_ETC/reboot-needed" ] && [ "$(awk '/btime/ {print $2}' /proc/stat)" -gt "$(stat -c %Y "$BOX_ETC/reboot-needed")" ]; then
  rm -f "$BOX_ETC/reboot-needed"      # restarted since it was written
fi
if [[ "$PROFILE" == pi* ]] && [ ! -f "$BOX_ETC/reboot-needed" ]; then
  grep -q 'memory' /sys/fs/cgroup/cgroup.controllers 2>/dev/null && ok "memory limits available (cgroup memory on)" \
    || warn "memory cgroup still off (check /proc/cmdline)"
fi
