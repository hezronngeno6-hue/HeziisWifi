"""
Billing engine — the single place where money turns into network access.

Everything else (portal, admin API, voucher tool) calls into this class.
The state machine for a purchase is:

    PENDING            STK push sent, waiting for the customer's PIN
      ├─ SUCCESS       payment confirmed -> grant() -> session ACTIVE
      ├─ CANCELLED     customer pressed cancel / timed out
      └─ FAILED        wrong PIN, insufficient funds, etc.

Access is granted by creating a hotspot user on the router carrying the plan's
uptime limit, then logging the device in. The router enforces the limit; this
service just records what happened and cleans up afterwards.
"""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any

from .db import Database, now_iso
from .mikrotik import MikroTikClient, MikroTikError
from .mpesa import CallbackResult, MpesaClient, MpesaError, mask_phone, normalise_phone

log = logging.getLogger("billing")


class BillingError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")


class BillingService:
    def __init__(self, cfg, db: Database, router: MikroTikClient, mpesa: MpesaClient):
        self.cfg = cfg
        self.db = db
        self.router = router
        self.mpesa = mpesa

    # ── bootstrap ────────────────────────────────────────────────────────
    def bootstrap(self) -> None:
        self.db.init()
        self.db.seed_plans(self.cfg.data.get("plans", []))
        try:
            self.router.ensure_profiles(self.cfg.plans)
        except MikroTikError as exc:
            log.warning("Could not sync router profiles: %s", exc)

    def plans(self) -> list[dict]:
        return [dict(r) for r in self.db.q(
            "SELECT * FROM plans WHERE active = 1 ORDER BY price ASC")]

    def plan(self, code: str) -> dict:
        row = self.db.one("SELECT * FROM plans WHERE code = ?", (str(code).upper(),))
        if not row:
            raise BillingError(f"Unknown plan: {code}")
        return dict(row)

    # ── purchase: M-Pesa ─────────────────────────────────────────────────
    def start_payment(self, phone: str, plan_code: str,
                      mac: str, ip: str | None = None) -> dict:
        plan = self.plan(plan_code)
        msisdn = normalise_phone(phone)
        mac = mac.upper()
        self.db.touch_device(mac, ip)

        # Safety first: never take money against a router we weren't configured for.
        try:
            self.router.ensure_mac_ok()
        except MikroTikError as exc:
            raise BillingError(str(exc)) from exc

        push = self.mpesa.initiate_stk_push(
            phone=msisdn,
            amount=plan["price"],
            account_ref=mac.replace(":", "")[-6:],
            description=f"{plan['name']} WiFi",
        )

        ts = now_iso()
        self.db.x(
            """INSERT INTO payments (merchant_request_id, checkout_request_id, phone,
                                     amount, plan_code, device_mac, status, created_at,
                                     updated_at, shortcode, till_type)
               VALUES (?,?,?,?,?,?,'PENDING',?,?,?,?)""",
            (push.merchant_request_id, push.checkout_request_id, msisdn,
             plan["price"], plan["code"], mac, ts, ts,
             self.mpesa.shortcode, self.mpesa.till_type),
        )
        self.db.audit(mac, "payment_started",
                      f"{plan['code']} KES {plan['price']} -> {mask_phone(msisdn)}")

        if self.mpesa.mock:
            self._schedule_mock_confirm(push.checkout_request_id, plan, msisdn, mac, ip)

        return {
            "checkout_request_id": push.checkout_request_id,
            "plan": plan["code"],
            "plan_name": plan["name"],
            "amount": plan["price"],
            "phone": mask_phone(msisdn),
            "message": push.customer_message or "Enter your M-Pesa PIN to complete payment.",
            "paid_to": f"{self.mpesa.till_label} {self.mpesa.shortcode}",
            "mock": self.mpesa.mock,
        }

    def _schedule_mock_confirm(self, checkout_id: str, plan: dict,
                               msisdn: str, mac: str, ip: str | None) -> None:
        """In mock mode, deliver a synthetic callback so the whole flow is testable."""
        def fire():
            payload = self.mpesa.mock_callback(checkout_id, plan["price"], msisdn, plan["code"])
            try:
                self.handle_callback(payload)
            except Exception:  # pragma: no cover - background thread
                log.exception("mock confirmation failed")

        threading.Timer(4.0, fire).start()

    def handle_callback(self, payload: dict) -> CallbackResult:
        """Process a Safaricom callback (or a mock one). Idempotent."""
        result = self.mpesa.parse_callback(payload)
        row = self.db.one(
            "SELECT * FROM payments WHERE checkout_request_id = ?",
            (result.checkout_request_id,),
        )
        if not row:
            log.warning("Callback for unknown checkout id %s", result.checkout_request_id)
            return result

        # Idempotency: Safaricom retries callbacks. Never grant twice.
        if row["status"] in ("SUCCESS", "CANCELLED", "FAILED"):
            log.info("Callback for %s already resolved as %s — ignoring",
                     result.checkout_request_id, row["status"])
            return result

        is_success = result.success

        # Grant access BEFORE marking the payment successful.
        # The portal polls this state and treats SUCCESS as "you're online". If we
        # flipped it first, a fast poll would see "paid" while no session existed
        # yet, and the customer would get an error page seconds after paying.
        grant_error = None
        if is_success:
            plan = self.plan(row["plan_code"])
            try:
                self.grant(row["device_mac"], self._ip_of(row["device_mac"]),
                           plan, payment_id=row["id"])
            except BillingError as exc:
                # The money is real, so the payment is still recorded as paid —
                # but loudly flagged, because access was not delivered.
                grant_error = str(exc)
                log.error("Paid but could not grant access: %s", exc)

        status = "SUCCESS" if is_success else (
            "CANCELLED" if result.result_code == 1032 else "FAILED")

        self.db.x(
            """UPDATE payments SET status = ?, result_code = ?, result_desc = ?,
                                   mpesa_receipt = ?, raw_callback = ?, updated_at = ?
               WHERE checkout_request_id = ?""",
            (status, result.result_code, result.result_desc, result.receipt,
             MpesaClient.dumps(payload), now_iso(), result.checkout_request_id),
        )
        self.db.audit(row["device_mac"], f"payment_{status.lower()}",
                      f"{result.result_desc} receipt={result.receipt or '-'}")
        if grant_error:
            self.db.audit(row["device_mac"], "paid_without_access", grant_error)
            raise BillingError(f"Payment {result.receipt or ''} taken but access was "
                               f"not granted: {grant_error}")
        return result

    def _ip_of(self, mac: str) -> str | None:
        row = self.db.one("SELECT ip FROM devices WHERE mac = ?", (mac.upper(),))
        return row["ip"] if row else None

    def payment_status(self, checkout_request_id: str) -> dict:
        row = self.db.one(
            "SELECT * FROM payments WHERE checkout_request_id = ?",
            (checkout_request_id,),
        )
        if not row:
            return {"status": "UNKNOWN"}
        session = self.db.one(
            "SELECT * FROM sessions WHERE payment_id = ? ORDER BY id DESC LIMIT 1",
            (row["id"],),
        )
        return {
            "status": row["status"],
            "result_desc": row["result_desc"],
            "receipt": row["mpesa_receipt"],
            "plan_code": row["plan_code"],
            "amount": row["amount"],
            "session": self._session_view(session) if session else None,
        }

    # ── granting access ──────────────────────────────────────────────────
    def grant(self, mac: str, ip: str | None, plan: dict,
              payment_id: int | None = None, voucher_code: str | None = None) -> dict:
        mac = mac.upper()
        started = datetime.now()
        expires = started + timedelta(minutes=int(plan["duration_minutes"]))

        # Extend rather than restart if the device already has time left.
        existing = self.db.active_session_for(mac)
        if existing:
            base = max(_parse(existing["expires_at"]), started)
            expires = base + timedelta(minutes=int(plan["duration_minutes"]))

        password = secrets.token_hex(8)
        username = self.router.username_for(mac)
        try:
            self.router.create_user(mac, plan, password)
            if ip:
                self.router.login_device(username, password, ip, mac)
        except MikroTikError as exc:
            # Payment is already taken — record the session anyway and flag loudly.
            log.error("Router refused access for %s: %s", mac, exc)
            self.db.audit(mac, "grant_router_error", str(exc))
            raise BillingError(
                "Payment received but the router did not accept the login. "
                "The session is recorded — check the router and the audit log."
            ) from exc

        with self.db.tx() as conn:
            cur = conn.execute(
                """INSERT INTO sessions (mac, ip, plan_code, payment_id, voucher_code,
                                         mikrotik_user, mikrotik_pass, started_at, expires_at,
                                         status, router_mac)
                   VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?)""",
                (mac, ip, plan["code"], payment_id, voucher_code, username, password,
                 _iso(started), _iso(expires), self.router.expected_mac or None),
            )
            session_id = cur.lastrowid
            if voucher_code:
                conn.execute(
                    """UPDATE vouchers SET used_at = ?, used_by_mac = ?, session_id = ?
                       WHERE code = ?""",
                    (now_iso(), mac, session_id, voucher_code),
                )

        self.db.audit(mac, "access_granted",
                      f"{plan['code']} until {_iso(expires)}"
                      + (f" voucher={voucher_code}" if voucher_code else ""))
        return self.get_session(session_id)

    def reconnect(self, mac: str, ip: str | None) -> dict:
        """Re-log a device that has a live session but got dropped from the hotspot."""
        mac = mac.upper()
        row = self.db.active_session_for(mac)
        if not row:
            raise BillingError("No active session for this device.")
        try:
            self.router.login_device(row["mikrotik_user"], row["mikrotik_pass"], ip or "", mac)
        except MikroTikError as exc:
            raise BillingError(f"Could not re-login: {exc}") from exc
        self.db.audit(mac, "reconnected", row["mikrotik_user"])
        return self._session_view(row)

    # ── vouchers ─────────────────────────────────────────────────────────
    def create_vouchers(self, plan_code: str, count: int, batch: str | None = None) -> list[str]:
        plan = self.plan(plan_code)
        batch = batch or datetime.now().strftime("%Y%m%d-%H%M")
        codes: list[str] = []
        with self.db.tx() as conn:
            for _ in range(int(count)):
                code = self._new_code()
                conn.execute(
                    "INSERT INTO vouchers (code, plan_code, batch, created_at) VALUES (?,?,?,?)",
                    (code, plan["code"], batch, now_iso()),
                )
                codes.append(code)
        self.db.audit("admin", "vouchers_created", f"{len(codes)} x {plan['code']} batch={batch}")
        return codes

    @staticmethod
    def _new_code() -> str:
        alphabet = "ACDEFGHJKLMNPQRTUVWXY34679"  # no look-alike characters
        return "".join(secrets.choice(alphabet) for _ in range(8))

    def redeem_voucher(self, code: str, mac: str, ip: str | None = None) -> dict:
        code = str(code).strip().upper()
        row = self.db.one("SELECT * FROM vouchers WHERE code = ?", (code,))
        if not row:
            raise BillingError("That code is not recognised.")
        if row["used_at"]:
            raise BillingError("That code has already been used.")
        plan = self.plan(row["plan_code"])
        self.db.touch_device(mac, ip)
        return self.grant(mac, ip, plan, voucher_code=code)

    # ── sessions ─────────────────────────────────────────────────────────
    def get_session(self, session_id: int) -> dict:
        row = self.db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return self._session_view(row) if row else {}

    def _session_view(self, row) -> dict:
        if not row:
            return {}
        data = dict(row)
        expires = _parse(data["expires_at"])
        remaining = int((expires - datetime.now()).total_seconds())
        data["remaining_seconds"] = max(0, remaining)
        data["remaining_human"] = _humanise(max(0, remaining))
        data["plan_name"] = (self.db.one(
            "SELECT name FROM plans WHERE code = ?", (data["plan_code"],)) or {"name": data["plan_code"]})["name"]
        data.pop("mikrotik_pass", None)  # never leak credentials outward
        return data

    def status_for_device(self, mac: str) -> dict:
        mac = mac.upper()
        device = self.db.one("SELECT * FROM devices WHERE mac = ?", (mac,))
        session = self.db.active_session_for(mac)
        return {
            "mac": mac,
            "known": bool(device),
            "label": device["label"] if device else None,
            "active": bool(session),
            "session": self._session_view(session) if session else None,
        }

    def sweep(self) -> dict:
        """Expire finished sessions and refresh byte counters from the router."""
        expired = 0
        for row in self.db.q(
            "SELECT * FROM sessions WHERE status = 'ACTIVE' AND expires_at <= ?",
            (now_iso(),),
        ):
            try:
                self.router.disconnect(row["mac"])
                self.router.remove_user(row["mikrotik_user"], missing_ok=True)
            except MikroTikError as exc:
                log.warning("sweep: router cleanup failed for %s: %s", row["mac"], exc)
            self.db.x(
                "UPDATE sessions SET status = 'EXPIRED', released_at = ? WHERE id = ?",
                (now_iso(), row["id"]),
            )
            self.db.audit(row["mac"], "session_expired", row["mikrotik_user"])
            expired += 1

        # byte counters
        try:
            live = {s["username"]: s for s in self.router.active_sessions()}
        except MikroTikError as exc:
            log.warning("sweep: cannot read active sessions: %s", exc)
            live = {}

        for row in self.db.q("SELECT * FROM sessions WHERE status = 'ACTIVE'"):
            usage = live.get(row["mikrotik_user"])
            if usage:
                self.db.x(
                    "UPDATE sessions SET bytes_in = ?, bytes_out = ? WHERE id = ?",
                    (usage.get("bytes_in", 0), usage.get("bytes_out", 0), row["id"]),
                )
                if row["ip"] != usage.get("ip"):
                    self.db.x("UPDATE sessions SET ip = ? WHERE id = ?", (usage.get("ip"), row["id"]))
        return {"expired": expired, "live": len(live)}

    # ── admin ────────────────────────────────────────────────────────────
    def destination(self) -> dict:
        """Where customer money lands — surfaced on the dashboard and setup screen."""
        info = self.mpesa.destination_summary()
        info["problems"] = self.mpesa.validate()
        return info

    def stats(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        def scalar(sql: str, params: tuple = ()) -> Any:
            row = self.db.one(sql, params)
            return list(row)[0] if row else 0

        return {
            "active_sessions": scalar(
                "SELECT COUNT(*) FROM sessions WHERE status='ACTIVE' AND expires_at > ?",
                (now_iso(),)),
            "devices_known": scalar("SELECT COUNT(*) FROM devices"),
            "revenue_today": round(scalar(
                "SELECT COALESCE(SUM(amount),0) FROM payments "
                "WHERE status='SUCCESS' AND created_at LIKE ?", (today + "%",)), 2),
            "revenue_total": round(scalar(
                "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='SUCCESS'"), 2),
            "payments_today": scalar(
                "SELECT COUNT(*) FROM payments WHERE created_at LIKE ?", (today + "%",)),
            "success_today": scalar(
                "SELECT COUNT(*) FROM payments WHERE status='SUCCESS' AND created_at LIKE ?",
                (today + "%",)),
            "vouchers_unused": scalar("SELECT COUNT(*) FROM vouchers WHERE used_at IS NULL"),
        }

    def active_sessions(self) -> list[dict]:
        rows = self.db.q(
            """SELECT s.*, p.name AS plan_name FROM sessions s
               JOIN plans p ON p.code = s.plan_code
               WHERE s.status='ACTIVE' AND s.expires_at > ?
               ORDER BY s.expires_at ASC""", (now_iso(),))
        return [self._session_view(r) for r in rows]

    def recent_payments(self, limit: int = 40) -> list[dict]:
        rows = self.db.q(
            "SELECT * FROM payments ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in rows]

    def recent_audit(self, limit: int = 60) -> list[dict]:
        rows = self.db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in rows]

    def kick(self, mac: str) -> None:
        mac = mac.upper()
        try:
            self.router.disconnect(mac)
        except MikroTikError as exc:
            log.warning("kick(%s): %s", mac, exc)
        self.db.x(
            "UPDATE sessions SET status='REVOKED', released_at=? "
            "WHERE mac=? AND status='ACTIVE'", (now_iso(), mac))
        self.db.audit("admin", "session_kicked", mac)


def _humanise(seconds: int) -> str:
    if seconds <= 0:
        return "expired"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
