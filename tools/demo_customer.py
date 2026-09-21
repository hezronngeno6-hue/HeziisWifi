"""
Customer-journey test — walk the whole purchase exactly as a customer does.

Drives the real HTTP API of a running service, so what you see here is what
your customers will get:

    1. device joins the WiFi            (MAC + IP from the hotspot)
    2. captive portal loads             GET  /
    3. customer picks a plan
    4. pays with M-Pesa                 POST /api/pay       -> Safaricom STK push
    5. Safaricom sends the result       POST /mpesa/callback
    6. access is granted                -> router command + session
    7. customer is online               GET  /api/device

    python -X utf8 -m tools.demo_customer
    python -X utf8 -m tools.demo_customer --phone 2547XXXXXXXX --plan DAY1

In sandbox, Safaricom's test MSISDN auto-confirms, so you can watch the whole
thing succeed without a handset.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SANDBOX_TEST_MSISDN = "254708374149"

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m")


def step(n: int, title: str) -> None:
    print(f"\n{BOLD}STEP {n}{OFF}  {title}")
    print("      " + "-" * 62)


def ok(msg: str) -> None:
    print(f"      {GREEN}OK{OFF}   {msg}")


def bad(msg: str) -> None:
    print(f"      {RED}FAIL{OFF} {msg}")


def info(msg: str) -> None:
    print(f"      {DIM}     {msg}{OFF}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--phone", default=SANDBOX_TEST_MSISDN,
                    help="customer MSISDN (default: Safaricom's sandbox test number)")
    ap.add_argument("--plan", default="DAY1")
    ap.add_argument("--wait", type=float, default=45.0)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    client = httpx.Client(base_url=base, timeout=25.0)
    mac = "AA:BB:CC:%02X:%02X:%02X" % (uuid.uuid4().int >> 96 & 0xFF,
                                       uuid.uuid4().int >> 88 & 0xFF,
                                       uuid.uuid4().int >> 80 & 0xFF)
    ip = "10.5.50." + str(20 + (uuid.uuid4().int % 200))

    print(f"\n{'=' * 72}")
    print(f"{BOLD}  CUSTOMER JOURNEY TEST{OFF}   {base}")
    print(f"  simulating a device joining the WiFi: {mac}  ({ip})")
    print(f"{'=' * 72}")

    # ── 1. service alive ────────────────────────────────────────────────
    step(1, "Service is up and the destination Till is configured")
    try:
        h = client.get("/healthz").json()
    except Exception as exc:
        bad(f"service not reachable at {base} — is uvicorn running? ({exc})")
        sys.exit(1)
    ok(f"service v{h.get('version')} responding")
    info(f"router: {h.get('router') or 'dry-run'}")

    # advance the device onto the network
    client.get("/api/device", params={"mac": mac})

    # ── 2. captive portal ───────────────────────────────────────────────
    step(2, "Customer's browser opens the captive portal")
    r = client.get("/", params={"mac": mac, "ip": ip})
    ok(f"portal loaded (HTTP {r.status_code}, {len(r.text)} bytes)")
    import re
    prices = re.findall(r'<div class="price">([^<]+)</div>', r.text)
    names = re.findall(r'<div class="name">([^<]+)</div>', r.text)
    for n, p in zip(names, prices):
        info(f"offered: {n:<10} {p.strip()}")

    # ── 3/4. choose a plan and pay ──────────────────────────────────────
    step(3, f"Customer picks {args.plan} and presses Pay")
    r = client.post("/api/pay", json={"mac": mac, "plan_code": args.plan,
                                      "phone": args.phone, "ip": ip})
    if r.status_code != 200:
        bad(f"payment could not start: HTTP {r.status_code} — {r.text[:200]}")
        sys.exit(1)
    pay = r.json()
    checkout = pay["checkout_request_id"]
    ok(f"{pay['plan_name']} for KES {pay['amount']:g} — charging {pay['phone']}")
    info(f"money destination : {pay['paid_to']}")
    info(f"checkout id       : {checkout}")
    info(f"message to customer: {pay['message']}")

    # ── 5. wait for Safaricom's callback ────────────────────────────────
    step(5, "Waiting for Safaricom to send the result to our callback URL")
    deadline = time.time() + args.wait
    status = {}
    seen = []
    while time.time() < deadline:
        status = client.get(f"/api/payment/{checkout}").json()
        st = status.get("status")
        if st not in seen:
            seen.append(st)
            info(f"state -> {st}")
        if st != "PENDING":
            break
        time.sleep(2)

    final = status.get("status")
    if final == "SUCCESS":
        ok(f"PAYMENT CONFIRMED — receipt {status.get('receipt')}")
        info(f"Safaricom said: {status.get('result_desc')}")
    elif final in ("CANCELLED", "FAILED"):
        bad(f"payment {final}: {status.get('result_desc')} (code not 0)")
        info("This still proves the callback arrived. For a success, the customer")
        info("must enter their PIN — try again with --phone <your number>.")
    else:
        bad(f"still {final} after {args.wait:g}s — no callback yet")
        info("Check the tunnel is running and public_base_url matches it.")
        sys.exit(1)

    # ── 6. access granted ───────────────────────────────────────────────
    step(6, "Access granted on the router")
    s = status.get("session") or {}
    if s:
        ok(f"session #{s.get('id')} created — plan {s.get('plan_name')}")
        info(f"expires  : {s.get('expires_at')}  ({s.get('remaining_human')} left)")
        info(f"router   : create user '{args.plan}' with limit-uptime, then log the "
             f"device in by MAC")
    else:
        info("no session (only created when the payment succeeds)")

    # ── 7. customer online ──────────────────────────────────────────────
    step(7, "Customer checks whether they are online")
    dev = client.get("/api/device", params={"mac": mac}).json()
    if dev.get("active"):
        ok(f"{mac} is ONLINE — {dev['session']['remaining_human']} remaining")
    else:
        info(f"{mac} is not online (payment did not succeed, so correctly no access)")

    print(f"\n{'=' * 72}")
    print("  Payments recorded for this run:")
    print(f"{'=' * 72}")
    import subprocess
    subprocess.run([sys.executable, "-X", "utf8", "-m", "tools.payments_report",
                    "--limit", "3"], cwd=str(ROOT))
    client.close()


if __name__ == "__main__":
    main()
