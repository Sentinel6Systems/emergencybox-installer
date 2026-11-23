# Shared helpers for install.sh and the modules. Sourced, not run.
# shellcheck shell=bash

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   OK    %s\n' "$*"; }
warn() { printf '   WARN  %s\n' "$*"; }
info() { printf '         %s\n' "$*"; }
die()  { printf '\n   STOP  %s\n' "$*" >&2; exit 1; }

# Fixed places (one disk, one filesystem). Not settings: every box looks the same inside.
export BOX_ROOT="/srv/box"
export CONTENT_DIR="/srv/box/content"        # what the owner adds: zims books media games apps
export MYFILES_DIR="/srv/box/myfiles"        # the family's own documents
export CONTENT_GROUP="boxcontent"            # every service that reads content is in it
export BOX_ETC="/etc/emergencybox"           # parts/, doctor.d/, firewall.d/, hotspot-ip
export WWW_DIR="/var/www/emergencybox"

CONFIG_KEYS="BOX_NAME BOX_SUBTITLE HOSTNAME DOMAIN WIFI_SSID WIFI_PASSWORD WIFI_CHANNEL COUNTRY
 HOTSPOT_IF HOTSPOT_IP ADMIN_PASSWORD USER_NAME USER_PASSWORD MAP_LAT MAP_LON MAP_ZOOM TIMEZONE
 MYFILES_MAX_GB SPACE_RESERVE_GB FIELDMAP_REPO FIELDMAP_BRANCH FIELDMAP_DIR JELLYFIN_LIBRARIES EMULATORJS_ARCHIVE
 PI_POWER_OFF_ON_HALT PI_USB_QUIRKS MODULES"

# load_config REPO_DIR : box.conf.default, then box.conf (if any), then check the values.
load_config() {
  local dir="$1" k
  # shellcheck source=/dev/null
  . "$dir/box.conf.default"
  if [ -f "$dir/box.conf" ]; then
    # shellcheck source=/dev/null
    . "$dir/box.conf"
    BOX_CONF="$dir/box.conf"
  else
    BOX_CONF="$dir/box.conf.default (no box.conf yet)"
  fi
  [ "$HOTSPOT_IP" = auto ] && HOTSPOT_IP="$(hotspot_ip)"
  for k in $CONFIG_KEYS; do export "${k?}"; done
  export BOX_CONF
  export ADDRESS="$HOSTNAME.$DOMAIN"
  export HOTSPOT_NET="${HOTSPOT_IP%.*}"       # first three parts, for the DHCP range
  export BOX_NAME_UPPER="${BOX_NAME^^}"
  export MAP_PRINT_ZOOM=$((MAP_ZOOM + 1))
  export OWNER="${SUDO_USER:-$(id -un)}"
  [ "$OWNER" = root ] && OWNER="$(stat -c %U "$dir")"
  check_config
}

# hotspot_ip : this box's own hotspot address. Picked once at random (10.A.B.1, A 64-250, so
# never the common 10.0/10.1/10.10 nets), away from every network the machine is on, then kept
# in $BOX_ETC/hotspot-ip so re-runs and other boxes don't change it.
hotspot_ip() {
  local f="$BOX_ETC/hotspot-ip" ip
  if [ -s "$f" ]; then cat "$f"; return; fi
  ip="$(python3 - <<'PY'
import ipaddress, random, subprocess
used = []
for cmd in (["ip", "-4", "-o", "addr", "show"], ["ip", "-4", "route", "show", "table", "all"]):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True).stdout.split()
    except OSError:
        continue
    for w in out:                  # every a.b.c.d or a.b.c.d/nn the machine has or routes to
        try:
            n = ipaddress.ip_network(w, strict=False)
        except ValueError:
            continue
        if n.version == 4 and n.prefixlen > 0:
            used.append(n)
for _ in range(200):
    net = ipaddress.ip_network(f"10.{random.randint(64, 250)}.{random.randint(0, 255)}.0/24")
    if not any(net.overlaps(u) for u in used):
        print(net.network_address + 1); break
PY
)"
  [ -n "$ip" ] || die "couldn't find a free 10.x.x.0/24 for the hotspot; set HOTSPOT_IP in box.conf"
  if [ "$(id -u)" -eq 0 ]; then install -d "$BOX_ETC"; echo "$ip" > "$f"; fi
  echo "$ip"
}

check_config() {
  local LC_ALL=C    # byte-wise patterns below, whatever the box's locale
  local name='^[[:alnum:] ._-]+$' word='^[[:alnum:]._-]+$' pw='^[[:graph:]]+$' num='^-?[0-9]+(\.[0-9]+)?$' bad=""
  [[ $BOX_NAME =~ $name ]]        || bad+=" BOX_NAME"
  [[ $BOX_SUBTITLE =~ $name ]]    || bad+=" BOX_SUBTITLE"
  [[ $HOSTNAME =~ ^[a-z0-9-]+$ ]] || bad+=" HOSTNAME(lower-case letters, numbers, -)"
  [[ $DOMAIN =~ ^[a-z0-9-]+$ ]]   || bad+=" DOMAIN"
  [[ $WIFI_SSID =~ $name && ${#WIFI_SSID} -le 32 ]] || bad+=" WIFI_SSID(max 32)"
  [[ $WIFI_PASSWORD =~ $pw && ${#WIFI_PASSWORD} -ge 8 && ${#WIFI_PASSWORD} -le 63 ]] || bad+=" WIFI_PASSWORD(8-63, no spaces)"
  [[ $WIFI_CHANNEL =~ ^([1-9]|1[0-3])$ ]] || bad+=" WIFI_CHANNEL"
  [[ $COUNTRY =~ ^[A-Z]{2}$ ]]    || bad+=" COUNTRY"
  [[ $ADMIN_PASSWORD =~ $pw ]]    || bad+=" ADMIN_PASSWORD"
  [[ $USER_NAME =~ $word ]]       || bad+=" USER_NAME"
  [[ $USER_PASSWORD =~ $pw ]]     || bad+=" USER_PASSWORD"
  [[ $MAP_LAT =~ $num && $MAP_LON =~ $num && $MAP_ZOOM =~ ^[0-9]+$ ]] || bad+=" MAP_LAT/MAP_LON/MAP_ZOOM"
  [[ $MYFILES_MAX_GB =~ ^[0-9]+$ ]]   || bad+=" MYFILES_MAX_GB"
  [[ $SPACE_RESERVE_GB =~ ^[0-9]+$ ]] || bad+=" SPACE_RESERVE_GB"
  local priv='^(10\.[0-9]+|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]+\.([1-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-4])$'
  [[ $HOTSPOT_IP =~ $priv ]] || bad+=" HOTSPOT_IP(auto, or a private address)"
  [[ -z $HOTSPOT_IF || $HOTSPOT_IF =~ ^[a-z0-9]+$ ]] || bad+=" HOTSPOT_IF"
  [[ $FIELDMAP_BRANCH =~ ^[A-Za-z0-9._/-]+$ && $FIELDMAP_BRANCH != -* ]] || bad+=" FIELDMAP_BRANCH"
  for v in "$WIFI_PASSWORD" "$ADMIN_PASSWORD" "$USER_PASSWORD"; do
    case "$v" in *[\'\"\\\`\$\<\>\&]*) bad+=" (passwords can't contain ' \" \\ \` \$ < > &)"; break ;; esac
  done
  [ -z "$bad" ] || die "Fix these settings in box.conf:$bad"
}

need_root() { [ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo ./install.sh"; }

is_pi()   { grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null; }
# online : a default route exists (the way out to the internet); uplink : its interface
online()  { [ -n "$(ip -4 route show default 2>/dev/null)" ]; }
uplink()  { ip -4 route show default 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1; }
ram_mb()  { awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo; }
free_gb() { df --output=avail -BG "${1:-/}" | tail -1 | tr -dc 0-9; }

# detect_profile : pi4 | pi5 | x86-laptop | x86-mini
detect_profile() {
  local model; model="$(tr -d '\0' 2>/dev/null < /proc/device-tree/model || true)"
  case "$model" in
    *"Raspberry Pi 5"*) echo pi5; return ;;
    *"Raspberry Pi 4"*|*"Raspberry Pi Compute Module 4"*) echo pi4; return ;;
    *"Raspberry Pi"*) echo pi4; return ;;
  esac
  local ct; ct="$(cat /sys/class/dmi/id/chassis_type 2>/dev/null || echo 0)"
  if ls /sys/class/power_supply/BAT* >/dev/null 2>&1 || [[ " 8 9 10 14 31 32 " == *" $ct "* ]]; then echo x86-laptop
  else echo x86-mini; fi
}

# service_user NAME HOME [content] : a system user for one service, with a home (some services
# write ~/.something on start). "content" also puts it in the content group so it can read
# whatever the owner copies in.
service_user() {
  local u="$1" home="$2"
  if ! id -u "$u" >/dev/null 2>&1; then
    useradd --system --home-dir "$home" --no-create-home --shell /usr/sbin/nologin --user-group "$u"
  fi
  install -d -o "$u" -g "$u" -m 750 "$home"
  [ "${3:-}" = content ] && usermod -a -G "$CONTENT_GROUP" "$u"
  return 0
}

# content_folder PATH [OWNER] : a content folder the owner can rsync into without sudo and every
# content service can read. setgid keeps new files in the group.
content_folder() {
  install -d -o "${2:-$OWNER}" -g "$CONTENT_GROUP" -m 2775 "$1"
  chown "${2:-$OWNER}:$CONTENT_GROUP" "$1"; chmod 2775 "$1"
}

# cmdline_edit "ADD..." "DEL_GLOB..." : make /boot/firmware/cmdline.txt (one line) contain every ADD
# token and no token matching a DEL glob. Returns 0 if nothing changed, 1 if it wrote (old copy kept).
cmdline_edit() {
  local f=/boot/firmware/cmdline.txt t g keep out=() changed=0 toks
  local -; set -f      # DEL globs match boot-line tokens, never file names
  [ -f "$f" ] || f=/boot/cmdline.txt
  [ -f "$f" ] || { warn "no cmdline.txt found"; return 0; }
  read -r -a toks < "$f"
  for t in "${toks[@]}"; do
    keep=1; for g in $2; do [[ $t == $g ]] && { keep=0; changed=1; }; done
    [ $keep = 1 ] && out+=("$t")
  done
  for t in $1; do [[ " ${out[*]} " == *" $t "* ]] || { out+=("$t"); changed=1; }; done
  [ $changed = 0 ] && return 0
  cp -p "$f" "$f.before-box.$(date +%Y%m%d%H%M%S)"
  printf '%s\n' "${out[*]}" > "$f"
  return 1
}

# render SRC DST : copy SRC to DST, replacing {{KEY}} with the exported setting.
# Stops if a {{KEY}} is left that isn't a setting (a typo in a template).
render() {
  python3 - "$1" "$2" <<'PY'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
def val(m):
    k = m.group(1)
    if k not in os.environ:
        sys.exit(f"render: {src} uses {{{{{k}}}}}, which is not a setting")
    return os.environ[k]
s = re.sub(r"\{\{([A-Z][A-Z0-9_]*)\}\}", val, s)
os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
open(dst, "w", encoding="utf-8").write(s)
PY
}

# render_tree SRC_DIR DST_DIR : render every text file; copy everything else as is.
render_tree() {
  local src="$1" dst="$2" f rel
  while IFS= read -r -d '' f; do
    rel="${f#"$src"/}"
    mkdir -p "$dst/$(dirname "$rel")"
    case "$f" in
      *.html|*.js|*.css|*.json|*.txt|*.py|*.conf|*.service) render "$f" "$dst/$rel" ;;
      *) cp -p "$f" "$dst/$rel" ;;
    esac
  done < <(find "$src" -type f -print0)
}

# install_if_changed SRC DST [MODE] : copy only when different. Returns 0 if it wrote, 1 if not,
# so callers restart a service only when its file changed.
install_if_changed() {
  if [ -f "$2" ] && cmp -s "$1" "$2"; then return 1; fi
  install -D -m "${3:-644}" "$1" "$2"
}

# nginx_snippet SRC NAME : render SRC into /etc/nginx/emergencybox.d/NAME (location blocks inside
# the box's site); undo it if nginx rejects it.
nginx_snippet() {
  local src="$1" dst="/etc/nginx/emergencybox.d/$2" bak="" new log
  install -d /etc/nginx/emergencybox.d
  new="$(mktemp)"; render "$src" "$new"
  if [ -f "$dst" ] && cmp -s "$new" "$dst"; then rm -f "$new"; ok "nginx: $2 (unchanged)"; return 0; fi
  [ -f "$dst" ] && { bak="$(mktemp)"; cp -p "$dst" "$bak"; }
  install -m 644 "$new" "$dst"; rm -f "$new"
  log="$(mktemp)"
  if nginx -t 2>"$log"; then
    systemctl reload nginx
    sleep 2        # a reload takes a moment; checks right after it can still hit the old config
    ok "nginx: $2"
  else
    cat "$log"
    if [ -n "$bak" ]; then cp -p "$bak" "$dst"; else rm -f "$dst"; fi
    die "nginx rejected $2; put back what was there before. nginx was not reloaded."
  fi
  rm -f "$log" "$bak"
  return 0
}

# doctor_check NAME LINE... : what box-doctor checks for this part (see files/doctor/box-doctor)
doctor_check() {
  local name="$1" t; shift
  t="$(mktemp)"; printf '%s\n' "# written by the $name module" "$@" > "$t"
  install_if_changed "$t" "$BOX_ETC/doctor.d/$name.check" || true; rm -f "$t"
}

# firewall_ports NAME LINE... : ports this part opens (tcp <port> | wan tcp <port> | iface <if> tcp <port>)
# Written only when it changes: a change makes box-firewall-reload.path reload the firewall.
firewall_ports() {
  local name="$1" t; shift
  t="$(mktemp)"; printf '%s\n' "# written by the $name module" "$@" > "$t"
  install_if_changed "$t" "$BOX_ETC/firewall.d/$name.ports" || true; rm -f "$t"
}

# part_installed NAME : tell the landing page this part is installed (boxd /api/parts).
# The date is the first install; re-runs leave it alone (no change on a second run).
part_installed() { install -d "$BOX_ETC/parts"; [ -f "$BOX_ETC/parts/$1" ] || date -Is > "$BOX_ETC/parts/$1"; }

# http_code PATH [curl args...] : status code of http://127.0.0.1PATH through nginx
http_code() { local p="$1"; shift; curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "Host: $ADDRESS" "$@" "http://127.0.0.1$p"; }

# apt_get ARGS... : apt-get that waits up to 5 minutes for another apt or dpkg run to finish
# (the daily apt job starts soon after the first boot, or the owner may be installing something).
apt_get() { DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 "$@"; }

apt_install() {
  local missing=() p
  for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  [ ${#missing[@]} -eq 0 ] && return 0
  online || die "No internet: ${missing[*]} must be downloaded. Connect Ethernet and run this again."
  local log=/var/log/emergencybox-apt.log
  if ! apt_get install -y -q "${missing[@]}" >"$log" 2>&1; then
    apt_get update -q >"$log" 2>&1 || true
    apt_get install -y -q "${missing[@]}" >"$log" 2>&1 \
      || { tail -20 "$log"; return 1; }
  fi
}
