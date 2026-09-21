"""
Payments report — reconcile your sales against the M-Pesa statement.

    python -X utf8 -m tools.payments_report             # last 20 payments
    python -X utf8 -m tools.payments_report --today
    python -X utf8 -m tools.payments_report --limit 50 --full-phone

Every row carries the shortcode and till type the sale was raised on, so a
production figure can be matched line-by-line against Safaricom's statement.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config          # noqa: E402
from app.db import Database                 # noqa: E402

STATUS_LABEL = {
    "SUCCESS": "PAID",
    "PENDING": "pending",
    "CANCELLED": "cancelled",
    "FAILED": "failed",
}


def mask(phone: str) -> str:
    p = str(phone or "")
    return p[:6] + "****" + p[-2:] if len(p) >= 8 else p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--full-phone", action="store_true", help="show full MSISDNs")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)
    db.init()
    currency = cfg.get("site.currency", "KES")

    where, params = "", ()
    if args.today:
        where = "WHERE created_at LIKE ?"
        params = (datetime.now().strftime("%Y-%m-%d") + "%",)

    rows = db.q(
        f"""SELECT * FROM payments {where} ORDER BY id DESC LIMIT ?""",
        params + (args.limit,))

    print(f"\nPayments — {db.path}")
    print(f"site: {cfg.get('site.name')}   environment: {cfg.get('mpesa.environment')}   "
          f"mock: {cfg.get('mpesa.mock')}")
    print("-" * 104)

    if not rows:
        print("  no payments recorded yet.")
    for r in rows:
        label = STATUS_LABEL.get(r["status"], r["status"])
        phone = r["phone"] if args.full_phone else mask(r["phone"])
        till = f"{r['shortcode'] or '?'}/{r['till_type'] or '?'}"
        receipt = r["mpesa_receipt"] or "-"
        print(f"  {r['created_at']}  {label:<9} {currency} {r['amount']:>7g}  "
              f"{phone:<15} {r['plan_code']:<8} till={till:<16} rcpt={receipt}")
        if r["result_desc"]:
            print(f"      {r['result_desc']}  (code {r['result_code']})")

    # ── summary ──────────────────────────────────────────────────────────
    def scalar(sql: str, p: tuple = ()) -> float:
        row = db.one(sql, p)
        return float(list(row)[0] or 0) if row else 0.0

    today = datetime.now().strftime("%Y-%m-%d") + "%"

    def count(status: str, like: str | None = None) -> float:
        if like:
            return scalar("SELECT COUNT(*) FROM payments WHERE status=? AND created_at LIKE ?",
                          (status, like))
        return scalar("SELECT COUNT(*) FROM payments WHERE status=?", (status,))

    def total(status: str, like: str | None = None) -> float:
        if like:
            return scalar("SELECT SUM(amount) FROM payments WHERE status=? AND created_at LIKE ?",
                          (status, like))
        return scalar("SELECT SUM(amount) FROM payments WHERE status=?", (status,))

    live = scalar("SELECT COUNT(*) FROM sessions WHERE status='ACTIVE' AND expires_at > ?",
                  (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),))

    print("-" * 104)
    print(f"  all time : {count('SUCCESS'):>5.0f} paid, {currency} {total('SUCCESS'):,.2f} collected")
    print(f"  today    : {count('SUCCESS', today):>5.0f} paid, {currency} {total('SUCCESS', today):,.2f}")
    print(f"  cancelled: {count('CANCELLED'):>5.0f}    "
          f"failed: {count('FAILED'):>5.0f}    "
          f"pending: {count('PENDING'):>5.0f}")
    print(f"  online now: {live:.0f} device(s)")
    if cfg.get("mpesa.mock", True):
        print("  NOTE: Test mode is on — these are simulated, not real M-Pesa transactions.")


if __name__ == "__main__":
    main()
