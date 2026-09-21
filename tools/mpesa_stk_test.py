"""
Fire one real STK push and show exactly what Safaricom says.

The app wraps errors, so this tool calls the same code path but prints the full
response body — which is where Safaricom puts the reason (e.g. "Invalid
CallBackURL", "Invalid PhoneNumber", "Bad Request - Invalid Amount").

    python -X utf8 -m tools.mpesa_stk_test
    python -X utf8 -m tools.mpesa_stk_test --phone 2547XXXXXXXX --amount 1

No secrets are printed: the password and keys stay hashed/hidden.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config                      # noqa: E402
from app.mpesa import MpesaClient, MpesaError, normalise_phone, mask_phone   # noqa: E402

SANDBOX_TEST_MSISDN = "254708374149"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default=SANDBOX_TEST_MSISDN)
    ap.add_argument("--amount", type=float, default=1.0)
    ap.add_argument("--ref", default="WIFITEST")
    args = ap.parse_args()

    cfg = load_config()
    client = MpesaClient(cfg)
    msisdn = normalise_phone(args.phone)

    print("\nSTK push diagnostic\n" + "-" * 66)
    print(f"  environment      : {client.env}  ({client.base})")
    print(f"  mock mode        : {client.mock}")
    print(f"  till type        : {client.till_type}  ({client.till_label})")
    print(f"  transaction type : {client.tx_type}")
    print(f"  shortcode        : {client.shortcode}")
    print(f"  passkey set      : {bool(client.passkey)} ({len(client.passkey)} chars)")
    print(f"  consumer key set : {bool(client.consumer_key)}")
    print(f"  callback URL     : {client.public_base_url}{client.callback_path}")
    print(f"  billing          : {mask_phone(msisdn)} for KES {args.amount:g}")
    print()

    problems = client.validate()
    if problems:
        print("Config problems found first:")
        for p in problems:
            print(f"  - {p}")
        print()

    # ── 1. can we authenticate at all? ───────────────────────────────────
    try:
        token = client.access_token()
        print(f"  1. OAuth          : OK (token {len(token)} chars)")
    except MpesaError as exc:
        print(f"  1. OAuth          : FAILED — {exc}")
        sys.exit(1)

    # ── 2. what does Safaricom say about the push? ───────────────────────
    try:
        result = client.initiate_stk_push(
            phone=msisdn, amount=args.amount, account_ref=args.ref,
            description="WiFi access test")
        print("  2. STK push       : ACCEPTED")
        print(f"     checkout id    : {result.checkout_request_id}")
        print(f"     merchant id    : {result.merchant_request_id}")
        print(f"     customer msg   : {result.customer_message}")
        print("\n  The integration is correct. The customer should get a PIN prompt,")
        print("  and Safaricom will POST the result to your callback URL.")
    except MpesaError as exc:
        print("  2. STK push       : REJECTED")
        print(f"\n  Safaricom said:\n     {exc}\n")
        print("  Common causes:")
        print("    - Invalid CallBackURL : must be a public https URL that answers;")
        print("                            your tunnel must still be running.")
        print("    - Invalid PhoneNumber : in sandbox the number must be registered as a")
        print("                            test MSISDN on the Daraja app. Try")
        print(f"                            --phone {SANDBOX_TEST_MSISDN} (Safaricom's test number).")
        print("    - Bad Request - Invalid Amount : must be a whole number >= 1.")
        print("    - Invalid Shortcode   : sandbox must be 174379.")
        print("    - Invalid TransactionType : Buy Goods needs CustomerBuyGoodsOnline.")
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
