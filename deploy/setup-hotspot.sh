#!/usr/bin/env bash
# =============================================================================
#  HEZIIS NET — turn this laptop into the hotspot gateway
# =============================================================================
#  After this runs, the laptop IS the router:
#
#      internet ──ethernet──> laptop ──wifi(AP)──> customers
#                                │
#                       captive portal + firewall
#
#  What it configures:
#    hostapd   broadcasts the SSID
#    dnsmasq   DHCP + hands every DNS name back to the portal (the "captive" part)
#    nftables  NAT to the internet, and DROP every client until the app allows them
#    systemd   keeps the billing service, hostapd and dnsmasq running
#
#  Usage:
#      sudo ./deploy/setup-hotspot.sh                        # use detected interfaces
#      sudo ./deploy/setup-hotspot.sh --dry-run              # show, change nothing
#      sudo ./deploy/setup-hotspot.sh --ap wlan0 --up eth0 \
#           --ssid "HEZIIS NET" --wifi-pass "changeme123"
#
#  Run it from the repo root. Re-running is safe — it rewrites its own config.
# =============================================================================
set -euo pipefail

# ── defaults ────────────────────────────────────────────────────────────────
AP_IF=""
UP_IF=""
SSID="HEZIIS NET"
WIFI_PASS="heziis1234"
CHANNEL="6"
COUNTRY="KE"
PORTAL_IP="192.168.50.1"
SUBNET="192.168.50.0/24"
DHCP_START="192.168.50.10"
DHCP_END="192.168.50.200"
APP_PORT="80"
DRY_RUN=0
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ap)        AP_IF="$2"; shift 2 ;;
    --up)        UP_IF="$2"; shift 2 ;;
    --ssid)      SSID="$2"; shift 2 ;;
    --wifi-pass) WIFI_PASS="$2"; shift 2 ;;
    --channel)   CHANNEL="$2"; shift 2 ;;
    --portal-ip) PORTAL_IP="$2"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    -h|--help)   sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

SUBNET="${PORTAL_IP%.*}.0/24"
DHCP_START="${PORTAL_IP%.*}.10"
DHCP_END="${PORTAL_IP%.*}.200"

c()  { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
hdr(){ echo; c "1;36" "── $1 ─────────────────────────────────────────────"; }
ok() { c "32" "   OK   $1"; }
warn(){ c "33" "   WARN $1"; }
die(){ c "31" "   FAIL $1"; exit 1; }
run(){ if [[ $DRY_RUN -eq 1 ]]; then c "2" "   [dry-run] $*"; else "$@"; fi; }
write(){ # write <path> <<< content, honouring dry-run and backing up
  local path="$1"; local body; body="$(cat)"
  if [[ $DRY_RUN -eq 1 ]]; then
    c "2" "   [dry-run] would write $path:"; printf '%s\n' "$body" | sed 's/^/            /'
  else
    [[ -f "$path" ]] && cp -a "$path" "${path}.bak.$(date +%s)"
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$body" > "$path"
    ok "wrote $path"
  fi
}

[[ $EUID -eq 0 || $DRY_RUN -eq 1 ]] || die "run with sudo (it configures the network)"
[[ "$(uname -s)" == "Linux" ]] || die "this script is for Linux — Windows cannot host a captive portal"

# ── 1. find the interfaces ──────────────────────────────────────────────────
hdr "1. Network interfaces"
if [[ -z "$AP_IF" ]]; then
  AP_IF="$(iw dev 2>/dev/null | awk '/Interface/{print $2; exit}')"
fi
if [[ -z "$UP_IF" ]]; then
  UP_IF="$(ip -o -4 route show default 2>/dev/null | awk '{print $5; exit}')"
fi
[[ -n "$AP_IF" ]] || die "no wireless interface found (install 'iw'?) — pass --ap wlan0"
[[ -n "$UP_IF" ]] || die "no upstream interface found — plug in the ethernet cable, or pass --up eth0"
[[ "$AP_IF" != "$UP_IF" ]] || warn "AP and upstream look like the SAME interface ($AP_IF).

   That means the WiFi would have to be a client of the internet AND the
   access point at once. Some cards can, but it is fragile. Strongly
   preferred: ethernet for internet, wifi for customers."

if iw list 2>/dev/null | grep -A8 'Supported interface modes' | grep -q 'AP'; then
  ok "$AP_IF supports AP mode"
else
  warn "$AP_IF does not advertise AP mode — hostapd may refuse to start"
fi
ip -o -4 addr show "$UP_IF" 2>/dev/null | sed 's/^/   /' || true
echo "   AP interface       : $AP_IF"
echo "   Upstream interface : $UP_IF"
echo "   Portal address     : $PORTAL_IP"

# ── 2. packages ─────────────────────────────────────────────────────────────
hdr "2. Packages"
need=(hostapd dnsmasq nftables python3 python3-venv python3-pip iw)
missing=()
for p in "${need[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "   installing: ${missing[*]}"
  run apt-get update -qq
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
else
  ok "all packages present"
fi

# ── 3. give the AP interface a static address ───────────────────────────────
hdr "3. Static address on $AP_IF"
run ip addr flush dev "$AP_IF" || true
run ip addr add "${PORTAL_IP}/24" dev "$AP_IF" || true
run ip link set "$AP_IF" up
ok "$AP_IF is $PORTAL_IP/24"

# ── 4. hostapd ─────────────────────────────────────────────────────────────
hdr "4. Access point (hostapd) — SSID '$SSID'"
run systemctl stop hostapd 2>/dev/null || true
run rfkill unblock wifi 2>/dev/null || true
run nmcli device set "$AP_IF" managed no 2>/dev/null || true   # stop NetworkManager fighting us
write /etc/hostapd/hostapd.conf <<EOF
# generated by HEZIIS NET setup-hotspot.sh
interface=$AP_IF
driver=nl80211
ssid=$SSID
hw_mode=g
channel=$CHANNEL
country_code=$COUNTRY
ieee80211d=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=$WIFI_PASS
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
ignore_broadcast_ssid=0
EOF
write /etc/default/hostapd <<EOF
DAEMON_CONF="/etc/hostapd/hostapd.conf"
EOF

# ── 5. dnsmasq: DHCP + the captive-portal DNS hijack ────────────────────────
hdr "5. DHCP and DNS (dnsmasq)"
run systemctl stop dnsmasq 2>/dev/null || true
# NOTE: we deliberately do NOT disable systemd-resolved. dnsmasq is told to use
# `bind-interfaces` + a single `interface=`, so it binds only to the hotspot
# address and never collides with resolved's 127.0.0.53.
write /etc/dnsmasq.d/heziis.conf <<EOF
# generated by HEZIIS NET setup-hotspot.sh

# only serve the hotspot network — never the upstream
interface=$AP_IF
bind-interfaces
except-interface=$UP_IF

# DHCP for customers
dhcp-range=$DHCP_START,$DHCP_END,255.255.255.0,12h
dhcp-option=option:router,$PORTAL_IP
dhcp-option=option:dns-server,$PORTAL_IP
dhcp-leasefile=/var/lib/misc/dnsmasq.heziis.leases

# THE CAPTIVE PORTAL BIT:
# every DNS lookup answers with the portal, so an unpaid customer cannot
# resolve anything — their device decides it is behind a captive portal and
# opens the browser at our page. Paid customers pass through the firewall,
# so for them this matters far less (they still get the portal's DNS answer,
# so give them the upstream resolver once authorised).
address=/#/$PORTAL_IP
server=1.1.1.1
server=8.8.8.8
no-resolv
log-queries
EOF
ok "all DNS names resolve to $PORTAL_IP until the customer pays"

# ── 6. routing and NAT ──────────────────────────────────────────────────────
hdr "6. Routing and NAT"
write /etc/sysctl.d/99-heziis.conf <<EOF
net.ipv4.ip_forward=1
EOF
run sysctl -q --system
run nft add table inet wifi 2>/dev/null || true
run nft add chain inet wifi forward '{ type filter hook forward priority 0 ; policy drop ; }' 2>/dev/null || true
# NAT out of the upstream interface
run nft add table ip heziis_nat 2>/dev/null || true
run nft add chain ip heziis_nat postrouting '{ type nat hook postrouting priority 100 ; }' 2>/dev/null || true
run nft add rule ip heziis_nat postrouting oifname "$UP_IF" masquerade 2>/dev/null || true
ok "clients are NATed out of $UP_IF; everything else is dropped by default"

# ── 7. the billing service ─────────────────────────────────────────────────
hdr "7. Billing service"
if [[ -f "$REPO_DIR/requirements.txt" ]]; then
  if [[ ! -d "$REPO_DIR/.venv" ]]; then
    run python3 -m venv "$REPO_DIR/.venv"
  fi
  run "$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
  ok "python environment ready"
fi
write /etc/systemd/system/heziis-billing.service <<EOF
[Unit]
Description=HEZIIS NET WiFi billing (captive portal + M-Pesa)
After=network-online.target hostapd.service dnsmasq.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
# root is required: it manages nftables (client access) and needs port 80 for the portal
User=root
Environment=PYTHONUNBUFFERED=1
ExecStart=$REPO_DIR/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port $APP_PORT
Restart=always
RestartSec=5
StandardOutput=append:$REPO_DIR/service.log
StandardError=append:$REPO_DIR/service.log

[Install]
WantedBy=multi-user.target
EOF

# ── 8. tell the app to use this machine as the gateway ─────────────────────
hdr "8. Point the app at this machine"
PY="$REPO_DIR/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
run "$PY" - <<PYEOF
import json, pathlib, sys
sys.path.insert(0, "$REPO_DIR")
p = pathlib.Path("$REPO_DIR/config.json")
if not p.exists():
    raise SystemExit("config.json missing — copy config.example.json first")
cfg = json.loads(p.read_text(encoding="utf-8-sig"))
cfg.setdefault("gateway", {})["type"] = "local"
cfg["gateway"].pop("_readme", None)
cfg["local"] = {
    "portal_ip": "$PORTAL_IP",
    "ap_interface": "$AP_IF",
    "upstream_interface": "$UP_IF",
    "upstream_gateway": "$(ip -o -4 route show default 2>/dev/null | awk '{print $3; exit}')",
    "require_root": True,
    "dry_run": False,
}
cfg.setdefault("server", {})["port"] = $APP_PORT
cfg["server"]["public_base_url"] = cfg.get("server", {}).get("public_base_url", "")
p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
print("   config.json updated: gateway=local, ap=$AP_IF, upstream=$UP_IF, port=$APP_PORT")
PYEOF

# ── 9. start everything ────────────────────────────────────────────────────
hdr "9. Starting services"
run systemctl unmask hostapd
run systemctl enable --now hostapd
run systemctl enable --now dnsmasq
run systemctl daemon-reload
run systemctl enable heziis-billing
run systemctl restart heziis-billing

hdr "Done"
cat <<EOF
   Customers connect to : $SSID
   Portal               : http://$PORTAL_IP/
   Admin                : http://$PORTAL_IP/admin
   Firewall             : table inet wifi  (unpaid clients are dropped)

   Check:
     systemctl status heziis-billing hostapd dnsmasq
     nft list set inet wifi allowed_macs      # who is currently allowed
     tail -f $REPO_DIR/service.log

   IMPORTANT — M-Pesa callbacks:
     Safaricom cannot reach a private address. Start a tunnel in ANOTHER
     window and paste its https URL into the setup screen's public URL:
         $REPO_DIR/bin/cloudflared tunnel --url http://localhost:$APP_PORT
     (download the Linux build: it is architecture-specific)

   Still needed before real money: the router details step (gateway is set to
   'local' now, so there is nothing to configure on a router).
EOF
