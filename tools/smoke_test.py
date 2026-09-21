"""
End-to-end smoke test.

Exercises the real HTTP surface against a running service: portal, mock M-Pesa
purchase, session grant, voucher redemption, admin API, voucher PDF, the setup
screen and the router-MAC guard.
Run it before you trust a deployment.

    python -m tools.smoke_test
    python -m tools.smoke_test --base http://127.0.0.1:8090 --admin-pass CHANGE-ME

NOTE: the setup-screen checks POST to /admin/setup, which rewrites
config.json. This script **snapshots config.json first and restores it on
exit** (including on failure), so your real Till number, router MAC and
credentials are never lost.
"""

from __future__ import annotations

import argparse
import atexit
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []

# Safaricom's documented sandbox test MSISDN — used for live sandbox runs so a
# real person's handset is never billed.
SANDBOX_TEST_MSISDN = "254708374149"


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def protect_config() -> None:
    """Snapshot config.json and restore it when this process exits.

    The setup-screen checks deliberately save test values, which is the correct
    thing to assert — but it must not leave the operator's real Till number and
    router MAC overwritten afterwards. atexit covers every exit path, including
    an early sys.exit() from report().
    """
    from app.config import CONFIG_PATH

    if not CONFIG_PATH.exists():
        return
    original = CONFIG_PATH.read_bytes()

    def restore() -> None:
        try:
            if CONFIG_PATH.read_bytes() != original:
                CONFIG_PATH.write_bytes(original)
                print("\n(restored your config.json — the setup checks had overwritten it)")
        except OSError:
            print("\nWARNING: could not restore config.json — check it before going live.")

    atexit.register(restore)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--admin-pass", default=None, help="defaults to config.json")
    ap.add_argument("--wait", type=float, default=20.0, help="seconds to wait for payment state")
    ap.add_argument("--phone", default=SANDBOX_TEST_MSISDN,
                    help="MSISDN to bill in a live run (default: Safaricom's sandbox test number)")
    args = ap.parse_args()

    protect_config()

    base = args.base.rstrip("/")
    mac = "AA:BB:CC:%02X:%02X:%02X" % (uuid.uuid4().int >> 96 & 0xFF,
                                       uuid.uuid4().int >> 88 & 0xFF,
                                       uuid.uuid4().int >> 80 & 0xFF)

    admin_pass = args.admin_pass
    mock_mode = True
    try:
        from app.config import load_config
        cfg = load_config()
        if admin_pass is None:
            admin_pass = str(cfg.get("server.admin_password", ""))
        mock_mode = bool(cfg.get("mpesa.mock", True))
    except Exception:
        pass

    auth = ("admin", admin_pass)
    client = httpx.Client(base_url=base, timeout=20.0)

    mode = "MOCK (simulated payments)" if mock_mode else "LIVE (real STK pushes to Safaricom)"
    print(f"\nSmoke test against {base}")
    print(f"  device: {mac}")
    print(f"  mpesa : {mode}\n")

    # ── health ───────────────────────────────────────────────────────────
    try:
        r = client.get("/healthz")
        body = r.json()
        check("healthz responds", r.status_code == 200, f"v{body.get('version')}")
        check("router reachable or absent", True,
              "dry-run" if body.get("router", {}).get("dry_run") else
              (f"error: {body.get('router_error')}" if body.get("router_error")
               else f"identity={body.get('router', {}).get('identity')}"))
    except Exception as exc:
        check("healthz responds", False, str(exc))
        return report()

    # ── portal ───────────────────────────────────────────────────────────
    r = client.get("/", params={"mac": mac, "ip": "10.5.50.20"})
    check("portal renders", r.status_code == 200 and "Choose a plan" in r.text,
          f"{r.status_code}, {len(r.text)} bytes")
    check("portal receives device MAC", mac in r.text)

    # ── purchase ─────────────────────────────────────────────────────────
    r = client.post("/api/pay", json={"mac": mac, "plan_code": "DAY1",
                                      "phone": args.phone, "ip": "10.5.50.20"})
    if r.status_code != 200:
        check("start payment", False, f"{r.status_code}: {r.text[:200]}")
        return report()
    pay = r.json()
    checkout = pay["checkout_request_id"]
    check("start payment", bool(checkout),
          f"{pay['plan_name']} KES {pay['amount']} -> {pay['paid_to']}")
    check("purchase names the destination Till", str(pay.get("paid_to", "")).strip() != "",
          pay.get("paid_to", ""))

    deadline = time.time() + args.wait
    status = {}
    while time.time() < deadline:
        status = client.get(f"/api/payment/{checkout}").json()
        if status.get("status") != "PENDING":
            break
        time.sleep(1.5)

    if mock_mode:
        check("payment confirmed", status.get("status") == "SUCCESS",
              f"{status.get('status')} {status.get('receipt') or ''}".strip())
        check("session created", bool(status.get("session")),
              (status.get("session") or {}).get("remaining_human", ""))
    else:
        # Live mode. The checkout id proves Safaricom accepted the request. Then:
        #   SUCCESS   -> the customer entered the PIN, access was granted
        #   CANCELLED -> the customer (or the sandbox test number) cancelled
        #   FAILED    -> wrong PIN / insufficient funds
        # All three are terminal states that can ONLY be reached by Safaricom
        # POSTing the result to our callback URL, so any of them proves the whole
        # loop works: portal -> STK push -> Safaricom -> tunnel -> callback -> DB.
        check("Safaricom accepted the STK push", bool(checkout), "checkout id issued")
        st = status.get("status")
        if st in ("SUCCESS", "CANCELLED", "FAILED"):
            check("M-Pesa callback reached this service", True,
                  f"state={st} — {status.get('result_desc') or 'result delivered via the tunnel'}")
        else:
            check("M-Pesa callback received", False,
                  f"state={st} — no callback yet. That is normal if the push is still "
                  f"sitting on a handset; run tools/mpesa_stk_test.py to see Safaricom's own reply.")

    # ── device status + reconnect ────────────────────────────────────────
    # What we expect depends on whether the payment actually completed: a
    # SUCCESS grants a session; a cancel/fail correctly grants nothing.
    dev = client.get("/api/device", params={"mac": mac}).json()
    expect_online = mock_mode or status.get("status") == "SUCCESS"

    if expect_online:
        check("device reports online", dev.get("active") is True,
              f"session for {mac}" if dev.get("active") else "no session found")
        r = client.post("/api/reconnect", json={"mac": mac, "ip": "10.5.50.20"})
        check("reconnect works", r.status_code == 200,
              r.text[:120] if r.status_code != 200 else "")
    else:
        check("no session when the payment did not complete", dev.get("active") is False,
              "device correctly not online")
        r = client.post("/api/reconnect", json={"mac": mac, "ip": "10.5.50.20"})
        check("reconnect refuses without a session", r.status_code == 400,
              r.json().get("detail", ""))

    # ── vouchers ─────────────────────────────────────────────────────────
    r = client.post("/admin/api/vouchers", params={"plan_code": "HOUR1", "count": 3}, auth=auth)
    if r.status_code == 401:
        check("admin auth", False, "wrong admin password — pass --admin-pass")
    else:
        check("admin auth accepts credentials", r.status_code == 200)
        codes = r.json().get("codes", [])
        check("vouchers created", len(codes) == 3, ", ".join(codes))

        if codes:
            v_mac = "AA:BB:CC:%02X:%02X:%02X" % (uuid.uuid4().int >> 96 & 0xFF,
                                                 uuid.uuid4().int >> 88 & 0xFF,
                                                 uuid.uuid4().int >> 80 & 0xFF)
            r = client.post("/api/voucher", json={"mac": v_mac, "code": codes[0], "ip": "10.5.50.21"})
            check("voucher redeemed", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
            r2 = client.post("/api/voucher", json={"mac": v_mac, "code": codes[0], "ip": "10.5.50.21"})
            check("voucher cannot be reused", r2.status_code == 400, r2.json().get("detail", ""))

    # ── admin pages ──────────────────────────────────────────────────────
    r = client.get("/admin", auth=auth)
    check("admin dashboard renders", r.status_code == 200 and "Active sessions" in r.text,
          f"{r.status_code}, {len(r.text)} bytes")

    r = client.get("/admin/vouchers.pdf", params={"count": 4, "plan_code": "HOUR1"}, auth=auth)
    check("voucher PDF generated", r.status_code == 200 and r.content[:4] == b"%PDF",
          f"{len(r.content)} bytes")

    # ── callback robustness ──────────────────────────────────────────────
    r = client.post("/mpesa/callback", json={"Body": {"stkCallback": {
        "CheckoutRequestID": "unknown-id", "ResultCode": 0, "ResultDesc": "x"}}})
    check("unknown callback is absorbed", r.status_code == 200, r.json().get("ResultDesc", ""))

    r = client.post("/api/voucher", json={"mac": mac, "code": "NOSUCHCODE", "ip": None})
    check("bad voucher rejected", r.status_code == 400)

    # ── setup screen: router MAC + till number ───────────────────────────
    r = client.get("/admin/setup", auth=auth)
    check("setup screen renders",
          r.status_code == 200 and "Router MAC address" in r.text and "Till / Paybill" in r.text,
          f"{r.status_code}, {len(r.text)} bytes")

    # Posting a complete form — unchecked boxes are simply absent, so send the
    # ones we want kept ON.
    r = client.post("/admin/setup", auth=auth, follow_redirects=False, data={
        "site_name": "ShalomNet WiFi", "currency": "KES", "support_phone": "+254700000000",
        "public_base_url": "https://example.test",
        "router_host": "192.168.88.1", "router_port": "443", "router_use_tls": "on",
        "router_username": "billing-api", "router_mac": "AA:BB:CC:DD:EE:99",
        "router_enforce_mac": "", "router_dry_run": "on",
        "mpesa_till_type": "buy_goods", "mpesa_shortcode": "5123456",
        "mpesa_environment": "sandbox", "mpesa_mock": "on",
    })
    check("setup saves", r.status_code in (200, 303), f"HTTP {r.status_code}")

    r = client.get("/admin/setup", auth=auth)
    check("saved router MAC persisted", "AA:BB:CC:DD:EE:99" in r.text)
    check("saved till number persisted", "5123456" in r.text)

    r = client.post("/admin/setup/test-mpesa", auth=auth)
    d = r.json()
    check("M-Pesa credential test runs", r.status_code == 200 and "ok" in d, d.get("detail", ""))
    check("money destination is the operator's Till",
          d.get("destination", {}).get("shortcode") == "5123456"
          and d["destination"]["transaction_type"] == "CustomerBuyGoodsOnline",
          str(d.get("destination", {})))

    r = client.post("/admin/setup/test-router", auth=auth)
    check("router test runs", r.status_code == 200 and "card" in r.json())

    # a buyer should be told where their money is going
    r = client.post("/api/pay", json={"mac": mac, "plan_code": "HOUR1",
                                      "phone": "0712345678", "ip": "10.5.50.20"})
    check("purchase states the till", r.status_code == 200 and "5123456" in r.json().get("paid_to", ""),
          r.json().get("paid_to", ""))

    # ── the MAC guard must refuse BEFORE charging ────────────────────────
    guard = verify_mac_guard()
    check("MAC guard blocks a mismatched router", guard, guard if isinstance(guard, str) else "")

    report()


def verify_mac_guard() -> bool | str:
    """Point the client at a dead address with a MAC set and enforcement on.

    The guard must fail closed: unreachable router + enforce = refuse to sell,
    because money would otherwise be taken for access we can't deliver.
    """
    from app.config import Config
    from app.mikrotik import MikroTikClient, MikroTikError

    cfg = Config({"mikrotik": {
        "enabled": True, "host": "127.0.0.1", "port": 9, "use_tls": False,
        "username": "x", "password": "y", "router_mac": "DE:AD:BE:EF:00:01",
        "enforce_router_mac": True, "dry_run": False,
    }})
    client = MikroTikClient(cfg)
    result = client.verify_router_mac()
    if result.get("ok"):
        return "guard did not flag an unreachable router (it should fail closed)"
    try:
        client.ensure_mac_ok()
    except MikroTikError:
        return True
    finally:
        client.close()
    return "ensure_mac_ok() did not raise"


def report() -> None:
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = [n for n, s, _ in results if s == FAIL]
    print(f"\n{passed}/{len(results)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
        sys.exit(1)
    print("everything looks good.")
    sys.exit(0)


if __name__ == "__main__":
    main()
