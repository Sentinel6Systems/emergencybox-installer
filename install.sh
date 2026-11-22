#!/usr/bin/env bash
# EmergencyBox installer: turns a fresh Raspberry Pi OS (or Debian/Ubuntu; arm64 or amd64)
# into an offline information box for a family. Everything installs on the system disk.
#
#   cp box.conf.default box.conf && nano box.conf     # optional: name, Wi-Fi, map area...
#   sudo ./install.sh                                 # everything, in order
#   sudo ./install.sh landing portal                  # only these parts
#   sudo ./install.sh --list                          # list the parts
#
# Every part is safe to re-run; a second run changes nothing. Re-running is also how
# you update a box after `git pull`. The box starts empty: add content afterwards.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export HERE
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

module_file() { ls "$HERE"/modules/[0-9][0-9]-"$1".sh 2>/dev/null | head -1; }

if [ "${1:-}" = "--list" ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,11p' "$0"; echo
  for f in "$HERE"/modules/[0-9][0-9]-*.sh; do
    [ -e "$f" ] || { echo "  (no parts yet)"; break; }
    n="$(basename "$f" .sh)"; printf '  %-10s %s\n' "${n#??-}" "$(sed -n '2s/^# //p' "$f")"
  done
  exit 0
fi

need_root
load_config "$HERE"
WANT=("$@")
[ ${#WANT[@]} -eq 0 ] && read -r -a WANT <<< "$MODULES"
[ ${#WANT[@]} -gt 0 ] || die "No parts to install (MODULES in box.conf.default is empty)"
for m in "${WANT[@]}"; do [ -n "$(module_file "$m")" ] || die "No part called '$m' (see: ./install.sh --list)"; done

LOG="/var/log/emergencybox-install.log"
echo "EmergencyBox installer"
echo "   settings: $BOX_CONF"
echo "   box:      $BOX_NAME at http://$ADDRESS ($HOTSPOT_IP), Wi-Fi $WIFI_SSID"
echo "   machine:  $(uname -m), $(ram_mb) MB RAM$(is_pi && echo ', Raspberry Pi')"
echo "   parts:    ${WANT[*]}"
echo "   log:      $LOG"
exec > >(tee -a "$LOG") 2>&1
echo "---- $(date -Is) install.sh ${WANT[*]}" >> "$LOG"

for m in "${WANT[@]}"; do
  set +e
  # shellcheck source=/dev/null
  ( set -euo pipefail; . "$(module_file "$m")" )
  rc=$?
  set -e
  case "$rc" in
    0) ;;
    3) echo; echo "Stopped after '$m' (see above). Do what it says, then run: sudo ./install.sh"; sync; exit 0 ;;
    *) echo; echo "The '$m' part failed (exit $rc). Fix the problem above and run: sudo ./install.sh $m"; sync; exit "$rc" ;;
  esac
done

sync    # a power cut right after an install must not leave freshly written files empty
echo
echo "All done: ${WANT[*]}"
if command -v nginx >/dev/null 2>&1 && systemctl is-active --quiet nginx; then
  echo "Join the $WIFI_SSID Wi-Fi and open http://$ADDRESS (or http://$HOTSPOT_IP)."
fi
if [ -f /etc/emergencybox/reboot-needed ]; then
  echo "Restart needed for: $(cat /etc/emergencybox/reboot-needed). Run: sudo reboot"
fi
