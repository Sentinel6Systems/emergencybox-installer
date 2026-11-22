# shellcheck shell=bash
# Retro games in the browser (EmulatorJS); the Games card shows once ROMs are added.

say "Games"
command -v nginx >/dev/null || die "needs the web module first (sudo ./install.sh web)"
. "$HERE/versions.lock"
WEBG="$WWW_DIR/games"                     # pages + emulator (program files)
ROMS="$CONTENT_DIR/games/roms"            # games the owner adds (content)
SYSTEMS="snes segaMD nes gb gbc gba segaMS segaGG"   # folder name = EmulatorJS core
EJS_URL="https://github.com/EmulatorJS/EmulatorJS/releases/download/v$EMULATORJS_VERSION/$EMULATORJS_VERSION.7z"

install -d -m 755 "$WEBG"
for f in index.html play.html; do install_if_changed "$HERE/files/games/$f" "$WEBG/$f" || true; done
content_folder "$CONTENT_DIR/games"; content_folder "$ROMS"
for s in $SYSTEMS; do content_folder "$ROMS/$s"; done
ok "pages in $WEBG; ROM folders ($SYSTEMS) in $ROMS"

# The release says which version it is (data/version.json); a changed pin installs the new one
have="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$WEBG/emulatorjs/data/version.json" 2>/dev/null || true)"
if [ -f "$WEBG/emulatorjs/data/loader.js" ] && [ "$have" = "$EMULATORJS_VERSION" ]; then
  ok "EmulatorJS $have already there"
else
  [ -n "$have" ] && info "EmulatorJS $have is there; versions.lock asks for $EMULATORJS_VERSION"
  command -v 7z >/dev/null || apt_install 7zip
  command -v 7z >/dev/null || die "Couldn't install 7-Zip (package 7zip)"
  TMP="$(mktemp -d /var/tmp/ejs.XXXX)"
  if [ -n "$EMULATORJS_ARCHIVE" ]; then
    ARC="$EMULATORJS_ARCHIVE"
  else
    ARC="$TMP/$EMULATORJS_VERSION.7z"
    info "downloading EmulatorJS $EMULATORJS_VERSION (289 MB)..."
    curl -fsSL --retry 3 -o "$ARC" "$EJS_URL" || die "Download failed: $EJS_URL"
  fi
  echo "$EMULATORJS_SHA256  $ARC" | sha256sum -c --quiet - || die "$ARC is not the EmulatorJS $EMULATORJS_VERSION release (checksum)"
  7z x -y -bd -o"$TMP/x" "$ARC" >/dev/null
  SRC="$(dirname "$(dirname "$(find "$TMP/x" -path '*/data/loader.js' | head -1)")")"
  [ -f "$SRC/data/loader.js" ] || die "The archive has no data/loader.js"
  rm -rf "$WEBG/emulatorjs"
  mv "$SRC" "$WEBG/emulatorjs"
  rm -rf "$TMP"
  ok "EmulatorJS $EMULATORJS_VERSION installed (works offline)"
fi
chmod -R a+rX "$WEBG"
nginx_snippet "$HERE/files/nginx/games-nginx.conf" games.conf

say "Checks"
[ "$(http_code /games/)" = 200 ] && ok "/games/" || die "/games/ not answering 200"
[ "$(http_code /games/emulatorjs/data/loader.js)" = 200 ] && ok "emulator files served" || die "emulator files not served"
[ "$(http_code /games/roms/)" = 200 ] && ok "ROM list served (the Games page reads it)" || die "/games/roms/ not answering 200"
n=$(find "$ROMS" -type f ! -name '.*' | wc -l)
ok "$n game files; add ROMs to $ROMS/<system>/"
part_installed games
doctor_check games "http /games/emulatorjs/data/loader.js nginx Games"
