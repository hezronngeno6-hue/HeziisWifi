"""
Safaricom M-Pesa Daraja client — STK Push (Lipa na M-Pesa Online).

Flow
----
    portal  -> initiate_stk_push(phone, amount, ref)  -> Safaricom pushes a PIN prompt
    user    -> enters PIN on their handset
    Safaricom -> POSTs to your CallbackURL           -> handle_callback(payload)
    billing -> authorises the device on the router

Two things trip people up:

1. **The callback URL must be public HTTPS.** Safaricom cannot reach your
   Windows PC on the LAN. Put a tunnel in front of it (see README:
   `cloudflared tunnel --url http://localhost:8090`) and use that URL.
2. **Passwords differ per environment.** The `passkey` and `shortcode` here must
   match the environment you set: sandbox shortcode 174379 for testing, your own
   Paybill/Till for production.

Set `mpesa.mock = true` to exercise the whole flow without Safaricom — the
portal behaves identically and payments auto-succeed after a short delay.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger("mpesa")

SANDBOX = "https://sandbox.safaricom.co.ke"
PRODUCTION = "https://api.safaricom.co.ke"


class MpesaError(RuntimeError):
    pass


def normalise_phone(raw: str) -> str:
    """Accept 07xx, 01xx, +2547xx, 2547xx, 7xx -> 2547XXXXXXXX (Daraja format)."""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if digits.startswith("254") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return "254" + digits[1:]
    if len(digits) == 9:
        return "254" + digits
    if digits.startswith("254") and len(digits) > 12:
        return digits[:12]
    raise MpesaError(f"Unrecognised phone number: {raw!r}")


def mask_phone(phone: str) -> str:
    return phone[:6] + "****" + phone[-2:] if len(phone) >= 8 else phone


@dataclass
class StkPushResult:
    checkout_request_id: str
    merchant_request_id: str
    customer_message: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class CallbackResult:
    checkout_request_id: str
    result_code: int
    result_desc: str
    amount: float | None = None
    receipt: str | None = None
    phone: str | None = None
    transaction_time: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.result_code == 0


class MpesaClient:
    def __init__(self, cfg):
        self.enabled = bool(cfg.get("mpesa.enabled", False))
        self.mock = bool(cfg.get("mpesa.mock", True))
        self.env = str(cfg.get("mpesa.environment", "sandbox")).lower()
        self.base = PRODUCTION if self.env == "production" else SANDBOX
        self.consumer_key = cfg.get("mpesa.consumer_key", "")
        self.consumer_secret = cfg.get("mpesa.consumer_secret", "")
        self.passkey = cfg.get("mpesa.passkey", "")
        self.shortcode = str(cfg.get("mpesa.shortcode", "174379"))
        # 'buy_goods' = a Till number (Lipa na M-Pesa Buy Goods)
        # 'paybill'   = a Paybill, which also wants an account number
        self.till_type = str(cfg.get("mpesa.till_type", "")).lower()
        if self.till_type not in ("buy_goods", "paybill"):
            # fall back to whatever transaction_type implies
            explicit = str(cfg.get("mpesa.transaction_type", ""))
            self.till_type = "buy_goods" if "BuyGoods" in explicit else "paybill"
        self.tx_type = ("CustomerBuyGoodsOnline" if self.till_type == "buy_goods"
                        else "CustomerPayBillOnline")
        if cfg.get("mpesa.transaction_type"):
            self.tx_type = cfg.get("mpesa.transaction_type")
        self.callback_path = cfg.get("mpesa.callback_path", "/mpesa/callback")
        self.ref_prefix = cfg.get("mpesa.account_reference_prefix", "WIFI")
        self.min_amount = float(cfg.get("mpesa.min_amount", 1))
        self.max_amount = float(cfg.get("mpesa.max_amount", 10000))
        self.public_base_url = str(cfg.get("server.public_base_url", "")).rstrip("/")

        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._http = httpx.Client(timeout=httpx.Timeout(30.0))

    def close(self) -> None:
        self._http.close()

    # ── auth ─────────────────────────────────────────────────────────────
    def access_token(self) -> str:
        if self.mock:
            return "mock-token"
        if self._token and time.time() < self._token_expiry - 30:
            return self._token

        url = f"{self.base}/oauth/v1/generate?grant_type=client_credentials"
        basic = base64.b64encode(
            f"{self.consumer_key}:{self.consumer_secret}".encode()
        ).decode()
        try:
            resp = self._http.get(url, headers={"Authorization": f"Basic {basic}"})
        except httpx.HTTPError as exc:
            raise MpesaError(f"Token request failed: {exc}") from exc
        if resp.status_code != 200:
            raise MpesaError(f"Token request -> {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3599))
        return self._token

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y%m%d%H%M%S")

    def _password(self, timestamp: str) -> str:
        raw = f"{self.shortcode}{self.passkey}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    # ── describing the destination ───────────────────────────────────────
    @property
    def till_label(self) -> str:
        return "Till (Buy Goods)" if self.till_type == "buy_goods" else "Paybill"

    def destination_summary(self) -> dict:
        """Where the money lands. Shown on the setup screen and written to each sale."""
        return {
            "till_type": self.till_type,
            "label": self.till_label,
            "shortcode": self.shortcode,
            "transaction_type": self.tx_type,
            "environment": self.env,
            "mock": self.mock,
            "ready": bool(self.mock or (self.consumer_key and self.consumer_secret and self.passkey)),
        }

    def validate(self) -> list[str]:
        """Human-readable list of what's missing. Empty list means good to go."""
        problems: list[str] = []
        if not self.shortcode.isdigit():
            problems.append(f"Shortcode/Till must be digits only (got {self.shortcode!r}).")
        if not self.mock:
            if not self.consumer_key:
                problems.append("Consumer Key is missing (from your Daraja app).")
            if not self.consumer_secret:
                problems.append("Consumer Secret is missing.")
            if not self.passkey:
                problems.append("Passkey is missing — it's bound to this shortcode.")
            if not self.public_base_url.startswith("https://"):
                problems.append("Public base URL must be https so Safaricom can reach the callback.")
        return problems

    def test_credentials(self) -> dict:
        """Ask Safaricom for an access token — the quickest proof the keys are right."""
        if self.mock:
            return {"ok": True, "detail": "Mock mode — no real credentials needed yet."}
        try:
            self._token = None
            token = self.access_token()
        except MpesaError as exc:
            return {"ok": False, "detail": str(exc)}
        return {"ok": True,
                "detail": f"Authenticated with Safaricom ({self.env}), {self.till_label} "
                          f"{self.shortcode}. Token starts {token[:6]}…"}

    # ── STK push ─────────────────────────────────────────────────────────
    def build_stk_payload(self, phone: str, amount: float, account_ref: str,
                          description: str = "WiFi access") -> dict:
        """The exact JSON body for an STK push, without sending it.

        Separated from initiate_stk_push() so it can be checked against
        Safaricom's official Postman collection — see tools/spec_check.py.
        """
        msisdn = normalise_phone(phone)
        amount = float(amount)
        ts = self._timestamp()
        return {
            "BusinessShortCode": self.shortcode,
            "Password": self._password(ts),
            "Timestamp": ts,
            "TransactionType": self.tx_type,
            "Amount": int(amount) if amount.is_integer() else amount,
            "PartyA": msisdn,
            "PartyB": self.shortcode,
            "PhoneNumber": msisdn,
            "CallBackURL": f"{self.public_base_url}{self.callback_path}",
            "AccountReference": f"{self.ref_prefix}-{account_ref}"[:12],
            "TransactionDesc": description[:60],
        }

    def initiate_stk_push(self, phone: str, amount: float, account_ref: str,
                          description: str = "WiFi access") -> StkPushResult:
        msisdn = normalise_phone(phone)
        amount = float(amount)
        if not (self.min_amount <= amount <= self.max_amount):
            raise MpesaError(f"Amount {amount} outside allowed range "
                             f"{self.min_amount}-{self.max_amount}")

        if self.mock:
            return self._mock_push(msisdn, amount, account_ref)

        if not self.consumer_key or not self.passkey:
            raise MpesaError("M-Pesa credentials missing (consumer_key / passkey). "
                             "Set mpesa.mock=true to test without them.")

        if not self.public_base_url.startswith("https://"):
            raise MpesaError("server.public_base_url must be a public https:// URL "
                             "for Safaricom to deliver the callback.")

        payload = self.build_stk_payload(phone, amount, account_ref, description)
        url = f"{self.base}/mpesa/stkpush/v1/processrequest"
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        try:
            resp = self._http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise MpesaError(f"STK push failed: {exc}") from exc
        if resp.status_code != 200:
            raise MpesaError(f"STK push -> {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        if str(data.get("ResponseCode")) != "0":
            raise MpesaError(f"STK push rejected: {data.get('ResponseDescription') or data}")

        log.info("STK push sent to %s for KES %s (%s)",
                 mask_phone(msisdn), amount, data.get("CheckoutRequestID"))
        return StkPushResult(
            checkout_request_id=data["CheckoutRequestID"],
            merchant_request_id=data.get("MerchantRequestID", ""),
            customer_message=data.get("CustomerMessage", ""),
            raw=data,
        )

    def _mock_push(self, msisdn: str, amount: float, account_ref: str) -> StkPushResult:
        token = "ws_CO_MOCK_" + secrets.token_hex(8)
        log.warning("[MOCK] STK push to %s for KES %s -> %s",
                    mask_phone(msisdn), amount, token)
        return StkPushResult(
            checkout_request_id=token,
            merchant_request_id="MOCK-" + secrets.token_hex(4),
            customer_message="Mock STK push — payment will auto-confirm.",
            raw={"mock": True},
        )

    def mock_callback(self, checkout_request_id: str, amount: float,
                      phone: str, plan_code: str) -> dict:
        """Build a callback payload identical in shape to Safaricom's, for testing."""
        return {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "MOCK-" + secrets.token_hex(4),
                    "CheckoutRequestID": checkout_request_id,
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": amount},
                            {"Name": "MpesaReceiptNumber",
                             "Value": "MOCK" + secrets.token_hex(4).upper()},
                            {"Name": "TransactionDate",
                             "Value": int(datetime.now().strftime("%Y%m%d%H%M%S"))},
                            {"Name": "PhoneNumber", "Value": int(normalise_phone(phone))},
                        ]
                    },
                }
            }
        }

    # ── callback parsing ─────────────────────────────────────────────────
    @staticmethod
    def parse_callback(payload: dict) -> CallbackResult:
        stk = (payload or {}).get("Body", {}).get("stkCallback")
        if not stk:
            raise MpesaError("Callback payload has no Body.stkCallback")

        meta = {
            item.get("Name"): item.get("Value")
            for item in (stk.get("CallbackMetadata") or {}).get("Item", [])
        }
        tx_time = meta.get("TransactionDate")
        return CallbackResult(
            checkout_request_id=str(stk.get("CheckoutRequestID", "")),
            result_code=int(stk.get("ResultCode", -1)),
            result_desc=str(stk.get("ResultDesc", "")),
            amount=float(meta["Amount"]) if meta.get("Amount") is not None else None,
            receipt=meta.get("MpesaReceiptNumber"),
            phone=str(meta["PhoneNumber"]) if meta.get("PhoneNumber") is not None else None,
            transaction_time=str(tx_time) if tx_time is not None else None,
            raw=payload,
        )

    @staticmethod
    def dumps(payload: dict) -> str:
        try:
            return json.dumps(payload)
        except (TypeError, ValueError):
            return "{}"
