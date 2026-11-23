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
# --- 2. A start map: the whole world at zoom 0-8 (versions.lock), so the map is never blank ----------
# It is also the world overview the Map Builder (/mapadmin/) puts under every region it builds;
# with it there, the Pi can build regions with "Include World Overview" (it can't make the world).
. "$HERE/versions.lock"
WORLD="$DATA/$(basename "$WORLD_MAP_URL")"
if [ ! -s "$WORLD" ]; then
  info "downloading the world map, zoom 0-8 ($WORLD_MAP_SIZE)..."
  if runuser -u "$OWNER" -- curl -fsSL --retry 3 --connect-timeout 20 -o "$WORLD.part" "$WORLD_MAP_URL" \
     && [ "$(sha256sum < "$WORLD.part" | cut -d' ' -f1)" = "$WORLD_MAP_SHA256" ]; then
    mv "$WORLD.part" "$WORLD"; chmod 664 "$WORLD"
    ok "world map: $(basename "$WORLD")"
  else
    rm -f "$WORLD.part"
    warn "couldn't download the world map (no internet, or the file didn't match versions.lock)."
    info "The map stays blank until you build a region at /mapadmin/. To retry: sudo ./install.sh maps"
  fi
else
  ok "world map: $(basename "$WORLD") (already there)"
fi
# The base map: the owner's own (built at /mapadmin/ or copied in) is never touched. Only a missing
# one, or fieldmap-server's empty placeholder (no tiles), becomes a copy of the world map.
tiles() { python3 -c "import sqlite3,sys; print(sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True).execute('select count(*) from tiles').fetchone()[0])" "$1" 2>/dev/null || echo 0; }
if [ -s "$WORLD" ] && { [ ! -s "$DATA/basemap.mbtiles" ] || [ "$(tiles "$DATA/basemap.mbtiles")" = 0 ]; }; then
  runuser -u "$OWNER" -- cp "$WORLD" "$DATA/basemap.mbtiles.part"
  mv "$DATA/basemap.mbtiles.part" "$DATA/basemap.mbtiles"; chmod 664 "$DATA/basemap.mbtiles"
  ok "base map: the world map, zoom 0-8 (build your own area at /mapadmin/ for more detail)"
fi
have=""; for f in basemap contours hillshade; do [ "$(tiles "$DATA/$f.mbtiles")" != 0 ] && have+=" $f"; done
ok "map data:${have:- none yet} (contours and hillshade come from a region build at /mapadmin/)"

# --- 3. Its installer, beside the box's nginx (Docker, tile server, map, routing, remote access) ----
info "running fieldmap-server's installer (first run: Docker, images, map build; 10-20 minutes on a Pi)..."
set +e
FIELDMAP_NGINX_DIR=/etc/nginx/emergencybox.d \
FIELDMAP_PORTS_FILE="$BOX_ETC/firewall.d/50-fieldmap.ports" \
FIELDMAP_SERVER_NAME="$ADDRESS" \
FIELDMAP_PAUSE_UNITS="jellyfin calibre-web kiwix-serve" \
  "$D/scripts/install-nginx.sh" < /dev/null 2>&1 | sed 's/^/         | /'
rc=${PIPESTATUS[0]}
set -e
[ "$rc" = 0 ] || die "fieldmap-server's installer stopped (exit $rc, above). Fix it and run: sudo ./install.sh maps"
ok "fieldmap-server installed"

say "Checks"
for p in /maps/styles.json /map/ /mapadmin/; do   # routing is optional (404 until its data is built)
  c="$(http_code "$p")"
  case "$c" in 200|301|302) ok "$p ($c)" ;; *) die "$p answered $c" ;; esac
done
doctor_check maps "service docker Docker (tile server)" "http /maps/styles.json docker Map tiles" "http /map/ nginx Map page" "http /mapadmin/ fieldmap-admin Map Builder"
info "Routing data is optional: $D/scripts/build-routing.sh <minlon> <minlat> <maxlon> <maxlat>"
