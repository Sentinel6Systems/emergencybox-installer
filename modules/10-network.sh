# shellcheck shell=bash
# Network: the Wi-Fi hotspot, box.lan and the phones' captive checks (DNS/DHCP), and the firewall.

say "Network (hotspot $WIFI_SSID at $HOTSPOT_IP)"
CON=emergencybox-hotspot
systemctl is-active --quiet NetworkManager || die "NetworkManager isn't running (it runs the hotspot)"
apt_install nftables iw rfkill bind9-dnsutils
ok "tools: nftables iw rfkill dig"

# --- 1. Which Wi-Fi adapter ---------------------------------------------------------------
IF="$HOTSPOT_IF"
if [ -z "$IF" ]; then
  for d in /sys/class/net/*; do [ -d "$d/wireless" ] && { IF="$(basename "$d")"; break; }; done
fi
[ -n "$IF" ] && [ -d "/sys/class/net/$IF/wireless" ] || die "No Wi-Fi adapter found (set HOTSPOT_IF in box.conf)"
iw list 2>/dev/null | grep -q '^[[:space:]]*\* AP$' || die "$IF can't run as an access point (no AP mode)"
ok "Wi-Fi adapter: $IF (AP mode supported)"

# Don't cut off the connection we're installing over
via="$(uplink)"
if [ "$via" = "$IF" ]; then
  die "This machine reaches the internet over $IF, which is about to become the hotspot. Connect Ethernet, then run this again."
fi

E="$(mktemp)"
printf 'HOTSPOT_IF=%s\nHOTSPOT_IP=%s\nHOTSPOT_CON=%s\n' "$IF" "$HOTSPOT_IP" "$CON" > "$E"
install_if_changed "$E" "$BOX_ETC/network.env" || true; rm -f "$E"
ok "settings: $BOX_ETC/network.env"

# --- 2. Names: box.lan, captive checks, no forwarding (NetworkManager's dnsmasq) -------------
systemctl is-enabled --quiet dnsmasq 2>/dev/null && { systemctl disable --now dnsmasq; warn "stopped the standalone dnsmasq service (it would fight over port 53)"; }
D="$(mktemp)"
render "$HERE/files/network/dnsmasq-emergencybox.conf" "$D"
{ grep -vE '^[[:space:]]*(#|$)' "$HERE/files/network/captive-hosts.list" | while read -r h; do echo "address=/$h/$HOTSPOT_IP"; done
  echo "# Windows asks this name and expects exactly this address"
  echo "address=/dns.msftncsi.com/131.107.255.255"; } >> "$D"   # s6-allow: Microsoft's fixed check address
install_if_changed "$D" /etc/NetworkManager/dnsmasq-shared.d/emergencybox.conf || true; rm -f "$D"
ok "DNS: $ADDRESS -> $HOTSPOT_IP, captive checks -> the box, nothing forwarded"

# --- 3. Firewall (before the hotspot comes up) ---------------------------------------------
fw=no
install_if_changed "$HERE/files/bin/box-firewall" /usr/local/sbin/box-firewall 755 && fw=yes
firewall_ports 10-network "tcp 80"
for u in box-firewall.service box-firewall-reload.service box-firewall-reload.path; do
  install_if_changed "$HERE/files/network/$u" "/etc/systemd/system/$u" && fw=yes
done
[ "$fw" = yes ] && systemctl daemon-reload
systemctl enable -q box-firewall
systemctl enable -q --now box-firewall-reload.path
if [ "$fw" = yes ] || ! systemctl is-active --quiet box-firewall; then systemctl restart box-firewall; fi
nft list table inet emergencybox >/dev/null 2>&1 || die "the firewall table isn't loaded (journalctl -u box-firewall)"
ok "firewall on: $(nft list chain inet emergencybox hotspot | grep -c accept) hotspot rules (box-firewall show)"
info "Ethernet: private addresses only; hotspot: DNS, DHCP, ping, web; nothing forwarded"

# --- 4. The hotspot --------------------------------------------------------------------------
rfkill unblock wifi 2>/dev/null || true
nmcli radio wifi on
host="${HOTSPOT_IP##*.}"
if [ "$host" -lt 10 ] || [ "$host" -gt 250 ]; then lo=10; hi=250
elif [ "$host" -le 128 ]; then lo=$((host + 1)); hi=254
else lo=2; hi=$((host - 1)); fi
args=(connection.interface-name "$IF" connection.autoconnect yes connection.autoconnect-priority 100
  802-11-wireless.ssid "$WIFI_SSID" 802-11-wireless.mode ap 802-11-wireless.band bg
  802-11-wireless.channel "$WIFI_CHANNEL" 802-11-wireless.powersave 2
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp
  wifi-sec.pmf disable wifi-sec.psk "$WIFI_PASSWORD"
  ipv4.method shared ipv4.addresses "$HOTSPOT_IP/24"
  ipv4.shared-dhcp-range "$HOTSPOT_NET.$lo,$HOTSPOT_NET.$hi" ipv4.shared-dhcp-lease-time 43200
  ipv6.method disabled)
# Restart the hotspot only when something changed: a restart drops every phone for a moment
want="$(printf '%s\n' "${args[@]}"; cat /etc/NetworkManager/dnsmasq-shared.d/emergencybox.conf)"
want="$(sha256sum <<< "$want" | cut -d' ' -f1)"
have="$(cat "$BOX_ETC/hotspot.sha256" 2>/dev/null || true)"
active="$(nmcli -g GENERAL.STATE con show "$CON" 2>/dev/null || true)"
if nmcli -t -f NAME con show | grep -qx "$CON"; then
  if [ "$want" != "$have" ]; then nmcli con modify "$CON" "${args[@]}"; ok "hotspot connection updated"; fi
else
  nmcli con add type wifi con-name "$CON" ifname "$IF" "${args[@]}" >/dev/null
  ok "hotspot connection created"
fi
if [ "$want" != "$have" ] || [ "$active" != activated ]; then
  nmcli con down "$CON" >/dev/null 2>&1 || true
  nmcli con up "$CON" >/dev/null || die "the hotspot didn't start (journalctl -u NetworkManager -n 50)"
  echo "$want" > "$BOX_ETC/hotspot.sha256"
  ok "hotspot started with the current settings"
else
  ok "hotspot unchanged and running (not restarted)"
fi

install_if_changed "$HERE/files/bin/box-hotspot-check" /usr/local/sbin/box-hotspot-check 755 || true
for u in box-hotspot-check.service box-hotspot-check.timer; do
  install_if_changed "$HERE/files/network/$u" "/etc/systemd/system/$u" || true
done
systemctl daemon-reload
systemctl enable -q --now box-hotspot-check.timer
ok "hotspot watchdog: checks 60 s after boot, then every 5 minutes"

# --- 5. Check the result ---------------------------------------------------------------------
sleep 3
iw dev "$IF" info | grep -q 'type AP' || die "$IF isn't in access-point mode"
ip -4 addr show "$IF" | grep -q "inet $HOTSPOT_IP/" || die "$IF doesn't have $HOTSPOT_IP"
ok "broadcasting \"$(iw dev "$IF" info | sed -n 's/.*ssid //p')\" on channel $(iw dev "$IF" info | sed -n 's/.*channel \([0-9]*\).*/\1/p'), address $HOTSPOT_IP"
q() { dig +short +time=2 +tries=1 @"$HOTSPOT_IP" "$1" A 2>/dev/null | head -1; }
[ "$(q "$ADDRESS")" = "$HOTSPOT_IP" ] || die "DNS: $ADDRESS doesn't answer $HOTSPOT_IP"
[ "$(q connectivitycheck.gstatic.com)" = "$HOTSPOT_IP" ] || die "DNS: captive checks don't point at the box"
[ -z "$(q example.com)" ] || die "DNS: example.com resolved; names are being forwarded"
ok "DNS: $ADDRESS and captive checks answer $HOTSPOT_IP; other names don't resolve"
nft list chain inet emergencybox forward | grep -q "iifname \"$IF\" drop" || die "firewall: hotspot forwarding isn't blocked"
ok "firewall on: hotspot forwarding blocked"
doctor_check network "hotspot Wi-Fi hotspot" "service box-firewall Firewall"
info "Phones joining \"$WIFI_SSID\" get $HOTSPOT_NET.$lo-$hi; the box is $HOTSPOT_IP"
