# shellcheck shell=bash
# Maps: fieldmap-server (map, map printer, routing, location sharing, remote access for the Fieldmap
# app), installed beside the box's nginx with its own installer. Map data: its own data/ folder.

say "Maps (fieldmap-server)"
command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
D="$FIELDMAP_DIR"
DATA="$D/data"     # fieldmap-server's own folder: its build scripts (routing, layers) write here too

# --- 1. The code: the latest of its branch on GitHub (it changes often), where nginx can read it ---
case "$D" in /home/*) die "FIELDMAP_DIR must not be in a home folder (nginx can't read those): use /opt/fieldmap-server" ;; esac
g() { runuser -u "$OWNER" -- git -C "$D" "$@"; }
if [ ! -d "$D/.git" ]; then
  install -d -o "$OWNER" -g "$OWNER" "$D"
  runuser -u "$OWNER" -- git clone -q --branch "$FIELDMAP_BRANCH" "$FIELDMAP_REPO" "$D" || die "couldn't clone $FIELDMAP_REPO (needs internet)"
fi
before="$(g rev-parse --short HEAD)"
if err="$(g fetch -q origin "$FIELDMAP_BRANCH" 2>&1)"; then
  if [ "$(g rev-parse HEAD)" != "$(g rev-parse FETCH_HEAD)" ] || [ "$(g branch --show-current)" != "$FIELDMAP_BRANCH" ]; then
    g checkout -q -B "$FIELDMAP_BRANCH" FETCH_HEAD || die "couldn't update fieldmap-server to the latest $FIELDMAP_BRANCH (see: git -C $D status)"
  fi
  now="$(g rev-parse --short HEAD)"
  [ "$now" = "$before" ] && how="the latest $FIELDMAP_BRANCH" || how="updated from $before"
else
  # Offline re-runs are normal for this box: keep what is there. A branch that isn't there is
  # a settings mistake, not being offline: stop, or updates would silently never come.
  case "$err" in *"couldn't find remote ref"*) die "fieldmap-server has no branch \"$FIELDMAP_BRANCH\" (FIELDMAP_BRANCH in box.conf)" ;; esac
  warn "couldn't fetch $FIELDMAP_BRANCH from $FIELDMAP_REPO, keeping the fieldmap-server that is there: $(tail -1 <<< "$err")"
  how="not checked for updates"
fi
ok "fieldmap-server $(g rev-parse --short HEAD) from $(g log -1 --format=%cs) in $D ($how)"
install -d -o "$OWNER" -g "$OWNER" "$DATA"
# Readable for every content service, so the owner's map files need no chmod
chgrp "$CONTENT_GROUP" "$DATA"; chmod 2775 "$DATA"

# The box has its own firewall (box-firewall, fed by the ports file). fieldmap-server's own guard
# table drops everything that isn't from a private address, which includes a new phone's DHCP
# request (from 0.0.0.0) on the hotspot: nobody new could join the Wi-Fi. Switch its guard off;
# its NetBird-interface rule stays.
[ -f "$D/.env" ] || install -o "$OWNER" -g "$OWNER" -m 600 /dev/null "$D/.env"
grep -q '^REMOTE_FIREWALL=' "$D/.env" || echo 'REMOTE_FIREWALL=off' >> "$D/.env"
ok "fieldmap-server's own firewall off (REMOTE_FIREWALL=off); the box's firewall is used"

part_installed maps
# --- 2. Map data: the owner adds it; without the three base layers there is nothing to serve ---------
missing=""; for f in basemap contours hillshade; do [ -s "$DATA/$f.mbtiles" ] || missing+=" $f.mbtiles"; done
if [ -n "$missing" ]; then
  info "no map data yet (missing:$missing): the Maps card shows \"Nothing added yet\"."
  info "Copy the .mbtiles files into $DATA (as $OWNER, no sudo needed), then run: sudo ./install.sh maps"
  doctor_check maps
  return 0
fi
ok "map data: $(cd "$DATA" && ls -1 ./*.mbtiles | sed 's|^\./||;s|\.mbtiles$||' | paste -sd' ' -) ($(du -sh --apparent-size "$DATA" | cut -f1))"

# --- 3. Its installer, beside the box's nginx (Docker, tile server, map, routing, remote access) ----
info "running fieldmap-server's installer (first run: Docker, images, map build; 10-20 minutes on a Pi)..."
set +e
FIELDMAP_NGINX_DIR=/etc/nginx/emergencybox.d \
FIELDMAP_PORTS_FILE="$BOX_ETC/firewall.d/50-fieldmap.ports" \
FIELDMAP_SERVER_NAME="$ADDRESS" \
  "$D/scripts/install-nginx.sh" < /dev/null 2>&1 | sed 's/^/         | /'
rc=${PIPESTATUS[0]}
set -e
[ "$rc" = 0 ] || die "fieldmap-server's installer stopped (exit $rc, above). Fix it and run: sudo ./install.sh maps"
ok "fieldmap-server installed"

say "Checks"
for p in /maps/styles.json /map/; do   # routing is optional (404 until its data is built)
  c="$(http_code "$p")"
  case "$c" in 200|301|302) ok "$p ($c)" ;; *) die "$p answered $c" ;; esac
done
doctor_check maps "service docker Docker (tile server)" "http /maps/styles.json docker Map tiles" "http /map/ nginx Map page"
info "Routing data is optional: $D/scripts/build-routing.sh <minlon> <minlat> <maxlon> <maxlat>"
