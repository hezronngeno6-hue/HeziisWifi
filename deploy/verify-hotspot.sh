#!/usr/bin/env bash
# =============================================================================
#  HEZIIS NET — hotspot health check
# =============================================================================
#  Run this after setup-hotspot.sh. It inspects the whole stack and prints a
#  PASS/FAIL report. If something is broken it tells you the exact command to
#  fix it, so you can paste the output back for a diagnosis instead of guessing.
#
#      sudo ./deploy/verify-hotspot.sh
#      sudo ./deploy/verify-hotspot.sh --ap wlan0 --portal-ip 192.168.50.1
# =============================================================================
set -uo pipefail

AP_IF=""; UP_IF=""; PORTAL_IP="192.168.50.1"; APP_PORT="80"; SSID=""
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ap) AP_IF="$2"; shift 2 ;;
    --up) UP_IF="$2"; shift 2 ;;
    --portal-ip) PORTAL_IP="$2"; shift 2 ;;
    --port) APP_PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

PASS=0; FAIL=0; WARN=0
G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; D=$'\033[2m'; O=$'\033[0m'

pass(){ printf '  %sPASS%s %s\n' "$G" "$O" "$1"; PASS=$((PASS+1)); }
fail(){ printf '  %sFAIL%s %s\n' "$R" "$O" "$1"; [[ -n "${2:-}" ]] && printf '       %sfix: %s%s\n' "$D" "$2" "$O"; FAIL=$((FAIL+1)); }
warn(){ printf '  %sWARN%s %s\n' "$Y" "$O" "$1"; WARN=$((WARN+1)); }
hdr(){ printf '\n%s%s%s\n' "$B" "$1" "$O"; printf '%s\n' "────────────────────────────────────────────────────────────"; }

have(){ command -v "$1" >/dev/null 2>&1; }

echo
printf '%sHEZIIS NET — hotspot verification%s\n' "$B" "$O"
echo "repo: $REPO_DIR"
echo "host: $(hostname)   kernel: $(uname -r)   $(date)"

# ── 0. environment ──────────────────────────────────────────────────────────
hdr "0. Environment"
if [[ $EUID -eq 0 ]]; then pass "running as root"; else fail "not root" "sudo $0"; fi
if [[ "$(uname -s)" == "Linux" ]]; then pass "Linux ($( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-?}" ))"; else fail "not Linux — a captive portal cannot work here" "boot Ubuntu"; fi
for pkg in hostapd dnsmasq nft python3; do
  if have "$pkg"; then pass "$pkg installed"; else fail "$pkg missing" "sudo apt install $pkg"; fi
done

# ── 1. interfaces ───────────────────────────────────────────────────────────
hdr "1. Network interfaces"
if [[ -z "$AP_IF" ]]; then AP_IF="$(iw dev 2>/dev/null | awk '/Interface/{print $2; exit}')"; fi
if [[ -z "$UP_IF" ]]; then UP_IF="$(ip -o -4 route show default 2>/dev/null | awk '{print $5; exit}')"; fi
echo "     AP=$AP_IF   upstream=$UP_IF   portal=$PORTAL_IP"

if [[ -n "$AP_IF" && -d "/sys/class/net/$AP_IF" ]]; then
  pass "AP interface $AP_IF exists"
else
  fail "AP interface ${AP_IF:-<none>} not found" "check 'ip link' / pass --ap wlan0"
fi

if have iw && [[ -n "$AP_IF" ]] && iw list 2>/dev/null | grep -A8 'Supported interface modes' | grep -q 'AP'; then
  pass "$AP_IF supports AP mode"
else
  warn "$AP_IF does not advertise AP mode (hostapd may refuse to start)"
fi

ap_ip="$(ip -o -4 addr show "${AP_IF:-none}" 2>/dev/null | awk '{print $4}' | head -1)"
if [[ "$ap_ip" == "${PORTAL_IP}/24" ]]; then
  pass "AP interface has the static address $ap_ip"
else
  fail "AP interface address is '${ap_ip:-none}', expected ${PORTAL_IP}/24" \
       "sudo ip addr add ${PORTAL_IP}/24 dev $AP_IF && sudo ip link set $AP_IF up"
fi

if [[ -n "$UP_IF" && "$UP_IF" == "$AP_IF" ]]; then
  warn "AP and upstream are the SAME interface — one radio doing both is fragile"
fi
if [[ -n "$UP_IF" ]] && ip -o -4 addr show "$UP_IF" 2>/dev/null | grep -q 'inet '; then
  pass "upstream $UP_IF has an address (ethernet plugged in)"
else
  fail "upstream ${UP_IF:-<none>} has no IPv4 address" "plug the ethernet cable into your router"
fi
if ip -o -4 route show default 2>/dev/null | grep -q .; then
  pass "default route present: $(ip -o -4 route show default | awk '{print $3, $5; exit}')"
else
  fail "no default route — no internet upstream" "check the ethernet cable / router"
fi

# ── 2. hostapd ──────────────────────────────────────────────────────────────
hdr "2. Access point (hostapd)"
if systemctl is-active --quiet hostapd; then
  pass "hostapd service is running"
else
  fail "hostapd is not running" "sudo journalctl -u hostapd -n 40  (then: sudo systemctl start hostapd)"
fi
if [[ -f /etc/hostapd/hostapd.conf ]]; then
  pass "hostapd.conf exists"
  SSID="$(awk -F= '/^ssid=/{print $2}' /etc/hostapd/hostapd.conf | head -1)"
  echo "     SSID: ${SSID:-<unset>}"
  grep -q '^interface=' /etc/hostapd/hostapd.conf && pass "hostapd.conf sets interface=$(awk -F= '/^interface=/{print $2}' /etc/hostapd/hostapd.conf)" \
    || fail "hostapd.conf has no interface= line" "re-run setup-hotspot.sh"
else
  fail "/etc/hostapd/hostapd.conf missing" "sudo ./deploy/setup-hotspot.sh"
fi
if have iw && [[ -n "$AP_IF" ]]; then
  if iw dev "$AP_IF" info 2>/dev/null | grep -q 'type AP'; then
    pass "$AP_IF is in AP mode"
  else
    fail "$AP_IF is not in AP mode" "sudo systemctl restart hostapd; check journalctl -u hostapd"
  fi
  if iw dev "$AP_IF" info 2>/dev/null | grep -q 'ssid'; then
    pass "SSID is being broadcast: $(iw dev "$AP_IF" info | awk -F'ssid ' '/ssid/{print $2; exit}')"
  else
    warn "no SSID visible on $AP_IF yet"
  fi
fi

# ── 3. DHCP + the captive DNS ───────────────────────────────────────────────
hdr "3. DHCP and captive DNS (dnsmasq)"
if systemctl is-active --quiet dnsmasq; then pass "dnsmasq is running"; else
  fail "dnsmasq is not running" "sudo journalctl -u dnsmasq -n 40"
fi
if [[ -f /etc/dnsmasq.d/heziis.conf ]]; then
  pass "dnsmasq config present"
  grep -q "address=/#/$PORTAL_IP" /etc/dnsmasq.d/heziis.conf \
    && pass "DNS wildcard points at the portal (address=/#/$PORTAL_IP)" \
    || fail "DNS wildcard missing — devices will not see a captive portal" "re-run setup-hotspot.sh"
  grep -q '^dhcp-range=' /etc/dnsmasq.d/heziis.conf \
    && pass "DHCP range: $(awk -F= '/^dhcp-range=/{print $2}' /etc/dnsmasq.d/heziis.conf)" \
    || fail "no dhcp-range configured" "re-run setup-hotspot.sh"
else
  fail "/etc/dnsmasq.d/heziis.conf missing" "sudo ./deploy/setup-hotspot.sh"
fi
if have ss && ss -lun 2>/dev/null | grep -q ":53"; then
  pass "something is listening on UDP :53 (DNS)"
else
  warn "nothing listening on UDP :53 — clients may get no DNS at all"
fi
# the actual captive-portal behaviour: ask for a random name, expect the portal IP
if have dig; then
  answer="$(dig +short +time=2 +tries=1 @$PORTAL_IP totally-unknown-name.example 2>/dev/null | tail -1)"
  if [[ "$answer" == "$PORTAL_IP" ]]; then
    pass "DNS hijack works: a nonsense name resolves to the portal ($answer)"
  else
    fail "DNS hijack returned '${answer:-nothing}' instead of $PORTAL_IP" "sudo systemctl restart dnsmasq"
  fi
else
  warn "dig not installed — cannot test the DNS hijack (sudo apt install dnsutils)"
fi

# ── 4. firewall ─────────────────────────────────────────────────────────────
hdr "4. Firewall and access control (nftables)"
if have nft; then
  if nft list table inet wifi >/dev/null 2>&1; then
    pass "nftables table 'inet wifi' exists"
    policy="$(nft list chain inet wifi forward 2>/dev/null | grep -o 'policy [a-z]*' | head -1)"
    if [[ "$policy" == "policy drop" ]]; then
      pass "forward chain policy is DROP (unpaid clients are blocked)"
    else
      fail "forward chain policy is '${policy:-unknown}', expected DROP" \
           "sudo systemctl restart heziis-billing  (it rebuilds the table)"
    fi
    if nft list set inet wifi allowed_macs >/dev/null 2>&1; then
      n="$(nft -j list set inet wifi allowed_macs 2>/dev/null | grep -o '"AA\|":"' | wc -l)"
      pass "allowed_macs set exists — currently $(nft list set inet wifi allowed_macs | grep -c 'elements') element group(s)"
      nft list set inet wifi allowed_macs | sed 's/^/       /'
    else
      fail "allowed_macs set missing" "sudo systemctl restart heziis-billing"
    fi
  else
    fail "nftables table 'inet wifi' missing — nothing is being blocked" \
         "sudo systemctl restart heziis-billing"
  fi
  if nft list table ip heziis_nat 2>/dev/null | grep -q masquerade; then
    pass "NAT masquerade rule present (clients can reach the internet)"
  else
    fail "no masquerade rule — paid clients would have no internet" "re-run setup-hotspot.sh"
  fi
else
  fail "nft not installed" "sudo apt install nftables"
fi
if [[ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" == "1" ]]; then
  pass "ip_forward is enabled"
else
  fail "ip_forward is disabled — no routing" "sudo sysctl -w net.ipv4.ip_forward=1"
fi

# ── 5. the billing service ──────────────────────────────────────────────────
hdr "5. Billing service"
if systemctl is-active --quiet heziis-billing; then
  pass "heziis-billing is running"
else
  fail "heziis-billing is not running" "sudo journalctl -u heziis-billing -n 40"
fi
if curl -fsS --max-time 8 "http://127.0.0.1:$APP_PORT/healthz" >/tmp/heziis_health.json 2>/dev/null; then
  pass "portal responds on 127.0.0.1:$APP_PORT/healthz"
  echo "       $(head -c 300 /tmp/heziis_health.json)"
else
  fail "no response on 127.0.0.1:$APP_PORT/healthz" "tail -n 40 $REPO_DIR/service.log"
fi
if curl -fsS --max-time 8 "http://$PORTAL_IP:$APP_PORT/" >/dev/null 2>&1; then
  pass "portal reachable on the hotspot address ($PORTAL_IP:$APP_PORT)"
else
  fail "portal not reachable on $PORTAL_IP:$APP_PORT" "check it is listening on 0.0.0.0, not 127.0.0.1"
fi
# The OS probes decide whether a customer sees "Sign in to network".
# A 302 to the portal is right; a bare 204 means the phone thinks it is online
# and will never show the payment page.
probe_check() {
  local path="$1" label="$2" expect="$3"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 \
          "http://$PORTAL_IP:$APP_PORT$path" 2>/dev/null)"
  if [[ "$code" == "$expect" ]]; then
    pass "$label probe $path -> $code"
  elif [[ "$code" == "204" ]]; then
    fail "$label probe $path -> 204 (phone thinks it IS online — no portal sheet)" \
         "the portal must answer these probes with a redirect, not 204"
  else
    fail "$label probe $path -> ${code:-no response} (expected $expect)" \
         "check the route is registered in app/main.py"
  fi
}
probe_check /generate_204    "Android" 302
probe_check /hotspot-detect.html "Apple" 302
probe_check /connecttest.txt "Windows" 302
probe_check /some/random/page "catch-all" 302

# ── 6. app configuration ────────────────────────────────────────────────────
hdr "6. App configuration"
PY="$REPO_DIR/.venv/bin/python"; [[ -x "$PY" ]] || PY=python3
if [[ -f "$REPO_DIR/config.json" ]]; then
  pass "config.json present"
  if have "$PY" || [[ -x "$PY" ]]; then
    "$PY" - "$REPO_DIR" <<'PYEOF' || true
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
cfg = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
def g(path, default=None):
    node = cfg
    for p in path.split("."):
        node = node.get(p, {}) if isinstance(node, dict) else {}
    return node or default
gw = g("gateway.type", "mikrotik")
print(f"       gateway.type     : {gw}")
print(f"       ap_interface     : {g('local.ap_interface')}")
print(f"       portal_ip        : {g('local.portal_ip')}")
print(f"       mpesa.mock       : {cfg.get('mpesa', {}).get('mock')}")
print(f"       mpesa.till       : {cfg.get('mpesa', {}).get('till_type')} {cfg.get('mpesa', {}).get('shortcode')}")
print(f"       public_base_url  : {cfg.get('server', {}).get('public_base_url')}")
if gw != "local":
    print("       !! gateway.type is not 'local' — customer access will NOT be enforced here.")
if cfg.get("mpesa", {}).get("mock"):
    print("       !! mpesa.mock is on — payments are simulated.")
url = cfg.get("server", {}).get("public_base_url", "")
if not url.startswith("https://"):
    print("       !! public_base_url is not https — Safaricom cannot deliver callbacks.")
PYEOF
  fi
  grep -q '"type": "local"' "$REPO_DIR/config.json" \
    && pass "gateway is set to local (this machine enforces access)" \
    || fail "gateway.type is not 'local'" "re-run setup-hotspot.sh, or set it in the setup screen"
else
  fail "config.json missing" "cp config.example.json config.json"
fi

# ── 7. end-to-end readiness ─────────────────────────────────────────────────
hdr "7. End-to-end readiness"
if have curl; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://$PORTAL_IP:$APP_PORT/admin" 2>/dev/null)"
  if [[ "$code" == "401" ]]; then
    pass "admin area requires a password (401 without credentials)"
  else
    warn "admin returned $code — check it is password-protected"
  fi
fi
if lsmod 2>/dev/null | grep -q iwlwifi; then
  pass "Intel WiFi driver (iwlwifi) loaded"
fi
if have rfkill; then
  if rfkill list wifi 2>/dev/null | grep -q 'Soft blocked: yes'; then
    fail "WiFi is soft-blocked" "sudo rfkill unblock wifi"
  else
    pass "WiFi radio is not blocked"
  fi
fi

# ── summary ─────────────────────────────────────────────────────────────────
hdr "Summary"
printf '  %s%d passed%s   %s%d failed%s   %s%d warnings%s\n' \
       "$G" "$PASS" "$O" "$R" "$FAIL" "$O" "$Y" "$WARN" "$O"
if [[ $FAIL -eq 0 ]]; then
  cat <<EOF

  Everything checks out. Connect a phone to "$SSID" — the portal should open
  by itself. If it does not, browse to http://$PORTAL_IP/ manually.

  To watch a real purchase:  tail -f $REPO_DIR/service.log
EOF
else
  cat <<EOF

  Fix the FAIL lines above, then run this again. Paste the whole output back
  and it can be diagnosed from the report alone.
EOF
fi
echo
exit $(( FAIL > 0 ? 1 : 0 ))
