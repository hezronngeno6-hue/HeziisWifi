"""
Pre-flight check — run this before you let customers pay real money.

    python -m tools.preflight

It looks for the mistakes people actually make: leaving the default admin
password in, pasting the whole Daraja credentials block into the Consumer Key
box, keeping the placeholder callback URL, picking the wrong Till type, and so
on. Secrets are never printed — only whether they look right.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config                      # noqa: E402
from app.config import ROOT as PROJECT_ROOT             # noqa: E402

OK, WARN, FAIL = "OK", "WARN", "FAIL"
rows: list[tuple[str, str, str]] = []


def add(level: str, area: str, message: str) -> None:
    rows.append((level, area, message))


def _looks_like_key(value: str) -> bool:
    """A Daraja consumer key/secret is a short alphanumeric blob."""
    v = (value or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9]{16,80}", v))


def check_secrets(cfg) -> None:
    admin = str(cfg.get("server.admin_password", "") or "")
    if admin == "CHANGE-ME" or len(admin) < 8:
        add(FAIL, "admin", "Admin password is still the default / too short — change it before going live.")
    else:
        add(OK, "admin", "Admin password has been changed.")

    secret = str(cfg.get("server.secret_key", "") or "")
    if secret == "CHANGE-ME-TOO":
        add(WARN, "admin", "server.secret_key is still the default.")

    for label, dotted in (("Consumer Key", "mpesa.consumer_key"),
                          ("Consumer Secret", "mpesa.consumer_secret"),
                          ("Passkey", "mpesa.passkey")):
        value = str(cfg.get(dotted, "") or "").strip()
        if not value:
            add(WARN, "mpesa", f"{label} is empty.")
            continue
        if len(value) > 120:
            add(FAIL, "mpesa", f"{label} is {len(value)} characters — far too long. You have probably "
                               f"pasted the whole credentials block instead of just the {label}.")
        elif not _looks_like_key(value):
            add(WARN, "mpesa", f"{label} has unexpected characters — double-check it was copied whole.")
        else:
            add(OK, "mpesa", f"{label} looks plausible ({len(value)} chars).")


def check_till(cfg) -> None:
    shortcode = str(cfg.get("mpesa.shortcode", "") or "").strip()
    till_type = str(cfg.get("mpesa.till_type", "") or "").strip().lower()
    env = str(cfg.get("mpesa.environment", "") or "").strip().lower()
    mock = bool(cfg.get("mpesa.mock", True))

    if not shortcode.isdigit():
        add(FAIL, "till", f"Till/Paybill number must be digits only (got {shortcode!r}).")
    else:
        add(OK, "till", f"Till/Paybill number is numeric ({len(shortcode)} digits).")
        if till_type == "buy_goods" and len(shortcode) < 6:
            add(WARN, "till", "A Buy Goods Till number is usually 6-7 digits — "
                              "5 digits or fewer is more typical of a Paybill. Check the type.")
        if till_type == "paybill" and len(shortcode) > 7:
            add(WARN, "till", "That looks long for a Paybill — is it actually a Buy Goods Till?")

    if till_type == "buy_goods":
        add(OK, "till", "Type = Buy Goods → CustomerBuyGoodsOnline.", )
    else:
        add(OK, "till", "Type = Paybill → CustomerPayBillOnline.")

    # Safaricom's sandbox test shortcode 174379 is a PAYBILL. Sending
    # CustomerBuyGoodsOnline to it returns "Bad Request - Invalid TransactionType".
    if env == "sandbox" and shortcode == "174379" and till_type == "buy_goods":
        add(FAIL, "till", "Sandbox shortcode 174379 is a Paybill — it rejects "
                          "CustomerBuyGoodsOnline with errorCode 400.002.02. Set till_type to "
                          "'paybill' for sandbox testing, then switch back to 'buy_goods' with "
                          "your real Till (5948231) for production.")
    if env == "production" and till_type == "buy_goods" and shortcode != "5948231":
        add(WARN, "till", f"Production Buy Goods expects your real Till 5948231, not {shortcode}.")

    expected_env = "sandbox" if mock else "production"
    # Test mode and environment are independent: you can run a *live* sandbox test
    # (mock off, environment sandbox) — that is the normal way to prove the flow
    # works before going anywhere near real money.
    if env not in ("sandbox", "production", ""):
        add(WARN, "till", f"environment is '{env}' — expected 'sandbox' or 'production'.")
    if mock:
        add(WARN, "till", "Test mode is ON — payments are simulated. Turn it off to run a live test.")
    elif env == "sandbox":
        add(OK, "till", "Live SANDBOX integration: real STK pushes to Safaricom's test till, "
                        "no real money, nothing reaches your Till.")
    elif env == "production":
        add(OK, "till", "Live PRODUCTION: real money will be charged and will reach your Till.")
    else:
        add(FAIL, "till", "Test mode is off but environment is unset.")


def check_callback(cfg) -> None:
    url = str(cfg.get("server.public_base_url", "") or "").strip()
    if not url:
        add(FAIL, "callback", "public_base_url is empty — Safaricom has nowhere to send the callback.")
        return
    if "safaricom.co.ke" in url or "stkpush" in url.lower():
        add(FAIL, "callback",
            f"public_base_url is set to Safaricom's own API endpoint ({url}). That is the "
            f"address the app calls — it is not your callback address. It must be YOUR public "
            f"https URL, e.g. https://your-tunnel.trycloudflare.com")
        return
    if not url.startswith("https://"):
        add(FAIL, "callback", f"public_base_url must start with https:// (got {url!r}).")
    elif "example" in url or "localhost" in url or "127.0.0.1" in url:
        add(FAIL, "callback", f"public_base_url is still a placeholder ({url}) — set your tunnel URL.")
    else:
        add(OK, "callback", f"Callback base URL is set ({url}).")


def check_router(cfg) -> None:
    enabled = bool(cfg.get("mikrotik.enabled", False))
    if not enabled:
        add(WARN, "router", "Router integration is off — nothing can be authorised.")
        return

    host = str(cfg.get("mikrotik.host", "") or "")
    mac = str(cfg.get("mikrotik.router_mac", "") or "").strip().upper()
    enforce = bool(cfg.get("mikrotik.enforce_router_mac", False))
    dry = bool(cfg.get("mikrotik.dry_run", True))
    password = str(cfg.get("mikrotik.password", "") or "")

    if password == "CHANGE-ME" or not password:
        add(FAIL, "router", "Router API password is empty or still the default.")
    else:
        add(OK, "router", "Router API password is set.")

    if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
        add(WARN, "router", f"router_mac is not a valid MAC (got {mac or 'empty'!r}). "
                            "Use the test button on the setup page to pick it from the router.")
    else:
        add(OK, "router", f"Router MAC set ({mac}).")
        if not enforce:
            add(WARN, "router", "Tick 'refuse to sell on a different router' to activate the MAC guard.")

    if dry:
        add(FAIL, "router", "Dry run is ON — router commands are only logged, nobody gets online.")
    else:
        add(OK, "router", "Dry run is off — the app will write to the router.")

    if host in ("192.168.1.1",) and not dry:
        add(WARN, "router", f"host is {host}. Many ISP-supplied routers (Airtel, Huawei, ZTE, Tozo/Tozed) "
                            "have no RouterOS API — the app needs your own MikroTik/OpenWrt router.")


def check_plans(cfg) -> None:
    plans = cfg.plans
    if not plans:
        add(FAIL, "plans", "No active plans — the portal will have nothing to sell.")
        return
    for p in plans:
        if float(p.get("price", 0)) <= 0:
            add(FAIL, "plans", f"Plan {p['code']} has a non-positive price.")
        if int(p.get("duration_minutes", 0)) <= 0:
            add(FAIL, "plans", f"Plan {p['code']} has no duration.")
    cheapest = min(float(p.get("price", 0)) for p in plans)
    add(OK, "plans", f"{len(plans)} active plan(s), cheapest {cheapest:g}.")


def main() -> None:
    cfg = load_config()
    check_secrets(cfg)
    check_till(cfg)
    check_callback(cfg)
    check_router(cfg)
    check_plans(cfg)

    order = {FAIL: 0, WARN: 1, OK: 2}
    rows.sort(key=lambda r: order[r[0]])

    print(f"\nPre-flight check — config: {PROJECT_ROOT / 'config.json'}\n")
    current = None
    for level, area, message in rows:
        if area != current:
            print(f"  {area.upper()}")
            current = area
        print(f"    [{level:4}] {message}")

    fails = sum(1 for r in rows if r[0] == FAIL)
    warns = sum(1 for r in rows if r[0] == WARN)
    print(f"\n{fails} blocking problem(s), {warns} warning(s).")
    if fails:
        print("This is NOT ready to take customer money.")
    elif warns:
        print("Functional, but review the warnings above before going live.")
    else:
        print("Looks ready.")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
