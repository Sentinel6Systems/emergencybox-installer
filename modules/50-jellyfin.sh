# shellcheck shell=bash
# Jellyfin (movies): install, /jellyfin/ address, accounts, libraries, direct play only. Installed empty.

say "Jellyfin"
command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
. "$HERE/versions.lock"
MEDIA="$CONTENT_DIR/media"
export MEDIA_DIR="$MEDIA"          # read by setup.py

# Written before the package goes in: the package starts Jellyfin at once.
# Restart on failure, and a memory cap on small machines.
RAM=$(ram_mb)
U="$(mktemp)"
{
  printf '# EmergencyBox\n[Service]\nRestart=on-failure\nRestartSec=5\n'
  if [ "$RAM" -lt 7000 ]; then printf 'MemoryHigh=%dM\nMemoryMax=%dM\n' $((RAM * 22 / 100)) $((RAM * 30 / 100)); fi
} > "$U"
dropin=no
install_if_changed "$U" /etc/systemd/system/jellyfin.service.d/emergencybox.conf && { dropin=yes; systemctl daemon-reload; }
rm -f "$U"
ok "restarts on failure$([ "$RAM" -lt 7000 ] && echo "; memory cap $((RAM * 30 / 100)) MB")"

if ! dpkg -s jellyfin-server >/dev/null 2>&1; then
  . /etc/os-release
  case "$ID" in
    debian|raspbian) REPO_OS=debian; SUITE="$VERSION_CODENAME" ;;
    ubuntu)          REPO_OS=ubuntu; SUITE="$VERSION_CODENAME" ;;
    *) die "Don't know which Jellyfin repository fits $PRETTY_NAME" ;;
  esac
  apt_install curl gnupg ca-certificates
  install -d -m 755 /etc/apt/keyrings
  key="$(mktemp)"
  curl -fsSL --retry 2 -o "$key" https://repo.jellyfin.org/jellyfin_team.gpg.key \
    || die "Can't reach repo.jellyfin.org: Jellyfin is installed from there (needs internet on Ethernet)"
  gpg --dearmor --yes -o /etc/apt/keyrings/jellyfin.gpg < "$key" || die "repo.jellyfin.org sent something that isn't its signing key"
  rm -f "$key"
  cat > /etc/apt/sources.list.d/jellyfin.sources <<SRC
Types: deb
URIs: https://repo.jellyfin.org/$REPO_OS
Suites: $SUITE
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/jellyfin.gpg
SRC
  apt-get update -q >/var/log/emergencybox-apt.log 2>&1 || { tail -20 /var/log/emergencybox-apt.log; die "apt-get update failed (Jellyfin repository unreachable?)"; }
  cand="$(apt-cache policy jellyfin-server | sed -n 's/^ *Candidate: //p')"
  case "$cand" in "$JELLYFIN_SERIES".*) ;; *) die "Jellyfin $cand is offered, but versions.lock allows $JELLYFIN_SERIES.x; check before changing the pin" ;; esac
  apt_install jellyfin || die "Couldn't install Jellyfin"
  ok "installed from repo.jellyfin.org ($REPO_OS $SUITE)"
fi
JV="$(dpkg-query -W -f='${Version}' jellyfin-server)"
case "$JV" in "$JELLYFIN_SERIES".*) ;; *) die "Jellyfin $JV is installed, but versions.lock allows $JELLYFIN_SERIES.x" ;; esac
apt-mark hold jellyfin jellyfin-server jellyfin-web >/dev/null 2>&1
ok "Jellyfin $JV, held (no surprise upgrades)"
# Jellyfin reads what the owner copies in, like every content service
if ! id -nG jellyfin | tr ' ' '\n' | grep -qx "$CONTENT_GROUP"; then usermod -a -G "$CONTENT_GROUP" jellyfin; dropin=yes; fi

content_folder "$MEDIA"
for lib in $JELLYFIN_LIBRARIES; do content_folder "$MEDIA/${lib%%:*}"; done
ok "media folders in $MEDIA: $(for l in $JELLYFIN_LIBRARIES; do printf '%s ' "${l%%:*}"; done)"

systemctl enable -q jellyfin
if [ "$dropin" = yes ] || ! systemctl is-active --quiet jellyfin; then systemctl restart jellyfin; fi

# Wait until Jellyfin answers: at /jellyfin once its address is set, at / on a fresh install.
# (Jellyfin 12 writes no network.xml on first start, so a file is not a usable signal.)
# While Jellyfin starts, a temporary "startup" server answers at / (even when the address is
# /jellyfin) and may redirect; only a plain 200 counts, and after a restart only /jellyfin counts.
# The startup server also reports "setup not done" and answers 503 to everything else, so the
# real server is up only when a second address (/System/Ping) is not 503 either.
jf_get() { curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:8096$1" 2>/dev/null || true; }
jf_code() {
  local c p; c="$(jf_get "$1/System/Info/Public")"
  [ "$c" = 200 ] || { echo "$c"; return; }
  p="$(jf_get "$1/System/Ping")"
  case "$p" in 503|000|"") echo starting ;; *) echo 200 ;; esac
}
jf_wait() {   # jf_wait SECONDS PREFIX... : prints the address of the first prefix that answers 200
  local secs="$1" t=0 b; shift
  while [ "$t" -lt "$secs" ]; do
    for b in "$@"; do
      [ "$(jf_code "$b")" = 200 ] && { echo "http://127.0.0.1:8096$b"; return 0; }
    done
    sleep 3; t=$((t + 3))
  done
  return 1
}
jf_write_netxml() {   # the address straight into network.xml, with Jellyfin stopped
  systemctl stop jellyfin
  python3 - /etc/jellyfin/network.xml <<'PY'
import os, sys, xml.etree.ElementTree as ET
p = sys.argv[1]
if os.path.exists(p):
    t = ET.parse(p); r = t.getroot()
else:
    r = ET.Element("NetworkConfiguration"); t = ET.ElementTree(r)
for k, v in (("BaseUrl", "/jellyfin"), ("EnableUPnP", "false")):
    e = r.find(k)
    if e is None:
        e = ET.SubElement(r, k)
    e.text = v
t.write(p, encoding="utf-8", xml_declaration=True)
PY
  chown jellyfin:jellyfin /etc/jellyfin/network.xml
  systemctl start jellyfin
}
info "waiting for Jellyfin to answer (its first start takes a minute or two on a Pi)..."
JF="$(jf_wait 360 /jellyfin "")" || { journalctl -u jellyfin -n 30 --no-pager; die "Jellyfin didn't answer within 6 minutes"; }
ok "Jellyfin answers at $JF"

say "Jellyfin accounts, libraries and address"
LOW=0; { is_pi || [ "$RAM" -lt 7000 ]; } && LOW=1
# setup.py exits 10 when it changed the address (restart needed), 11 when there's no settings API
set +e
JF_URL="$JF" JF_BASEURL=/jellyfin JF_LOW_POWER=$LOW python3 "$HERE/files/jellyfin/setup.py"
rc=$?
set -e
case "$rc" in 0|10|11) ;; *) die "Jellyfin's automatic setup didn't finish (above). Fix it and run: sudo ./install.sh jellyfin" ;; esac
if [ "$rc" = 0 ] && [ "$(jf_code /jellyfin)" = 200 ]; then
  ok "address already /jellyfin"
else
  if [ "$rc" = 11 ]; then jf_write_netxml; else systemctl restart jellyfin; fi
  info "Jellyfin is restarting with its new address..."
  if jf_wait 240 /jellyfin >/dev/null; then
    ok "address set to /jellyfin (Jellyfin restarted)"
  else
    info "the saved setting didn't take effect; writing it into network.xml instead"
    jf_write_netxml
    if jf_wait 240 /jellyfin >/dev/null; then
      ok "address set to /jellyfin in network.xml (Jellyfin restarted)"
    else
      info "answers: /jellyfin -> $(jf_code /jellyfin), / -> $(jf_code "")"
      cat /etc/jellyfin/network.xml 2>/dev/null | head -20 || true
      journalctl -u jellyfin -n 25 --no-pager || true
      die "Jellyfin doesn't answer at /jellyfin"
    fi
  fi
fi
ok "admin / (ADMIN_PASSWORD), hidden from the sign-in screen; $USER_NAME / (USER_PASSWORD), direct play only"

nginx_snippet "$HERE/files/nginx/jellyfin-nginx.conf" jellyfin.conf

say "Checks"
c="$(http_code /jellyfin/System/Info/Public)"
[ "$c" = 200 ] && ok "/jellyfin/ answers through nginx" || die "/jellyfin/System/Info/Public answered $c"
part_installed movies
doctor_check jellyfin "service jellyfin Movies (Jellyfin)" "http /jellyfin/System/Info/Public jellyfin Movies page"
info "Add movies to $MEDIA/<library>/ (H.264 + AAC in .mp4 plays everywhere); new files show up within a minute"
