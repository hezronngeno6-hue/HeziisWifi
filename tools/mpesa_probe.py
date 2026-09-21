"""
M-Pesa connection probe.

Answers the question that trips everyone up: *which environment do my Daraja
credentials actually belong to?* Sandbox apps and production apps are separate —
using production keys against the sandbox endpoint (or vice versa) fails, and the
error message Safaricom returns is unhelpful.

    python -X utf8 -m tools.mpesa_probe

It tries the OAuth endpoint for both environments with your Consumer Key/Secret,
reports which one accepts them, and checks the shortcode/passkey consistency
rules. Tokens and credentials are never printed.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config, save_config                  # noqa: E402

# Safaricom's published sandbox test values (documented publicly, safe to name).
SANDBOX_SHORTCODE = "174379"
SANDBOX_PASSKEY = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"

BASES = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}


def try_auth(base: str, key: str, secret: str) -> tuple[bool, str]:
    url = f"{base}/oauth/v1/generate?grant_type=client_credentials"
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    try:
        r = httpx.get(url, headers={"Authorization": f"Basic {basic}"}, timeout=25)
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"
    if r.status_code != 200:
        detail = ""
        try:
            body = r.json()
            detail = body.get("errorMessage") or body.get("error_description") or ""
        except ValueError:
            detail = r.text[:120]
        return False, f"HTTP {r.status_code} {detail}".strip()
    try:
        token = r.json().get("access_token", "")
    except ValueError:
        return False, "HTTP 200 but the body was not JSON"
    if not token:
        return False, "HTTP 200 but no access_token in the response"
    return True, f"accepted (token length {len(token)})"


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Check which Daraja environment your keys belong to.")
    ap.add_argument("--apply-sandbox", action="store_true",
                    help=f"also write the sandbox shortcode {SANDBOX_SHORTCODE} and "
                         f"Safaricom's published sandbox passkey")
    args = ap.parse_args()

    cfg = load_config()
    key = str(cfg.get("mpesa.consumer_key", "") or "").strip()
    secret = str(cfg.get("mpesa.consumer_secret", "") or "").strip()
    passkey = str(cfg.get("mpesa.passkey", "") or "").strip()
    shortcode = str(cfg.get("mpesa.shortcode", "") or "").strip()
    configured_env = str(cfg.get("mpesa.environment", "") or "").strip().lower()

    print("\nM-Pesa probe\n" + "-" * 62)
    if not key or not secret:
        print("Consumer Key and/or Secret are empty — nothing to test.")
        sys.exit(1)
    print(f"  Consumer Key   : {len(key)} chars")
    print(f"  Consumer Secret: {len(secret)} chars")
    print(f"  Passkey        : {'set, ' + str(len(passkey)) + ' chars' if passkey else 'EMPTY'}")
    print(f"  Shortcode      : {shortcode or '(none)'}")
    print(f"  Configured env : {configured_env or '(unset)'}")

    print("\nTrying to authenticate against both environments:\n")
    accepted: list[str] = []
    for env, base in BASES.items():
        ok, detail = try_auth(base, key, secret)
        print(f"  {env:11} {base:38} -> {'ACCEPTED' if ok else 'rejected'}: {detail}")
        if ok:
            accepted.append(env)

    print("\n" + "-" * 62)
    if not accepted:
        print("Neither environment accepted these credentials.")
        print("Re-copy the Consumer Key and Consumer Secret from your app on")
        print("developer.safaricom.co.ke — and make sure the app is not still")
        print("awaiting approval (unapproved apps cannot authenticate).")
        sys.exit(1)

    if len(accepted) == 2:
        print("Both environments accepted the keys (unusual — check the app type).")
    else:
        env = accepted[0]
        print(f"These credentials belong to the {env.upper()} environment.")
        if env != configured_env:
            print(f"  -> config.json says '{configured_env}'. It should be '{env}'.")
        else:
            print("  -> matches what config.json says. Good.")

    wrong = []
    if "sandbox" in accepted:
        if shortcode != SANDBOX_SHORTCODE:
            wrong.append(
                f"Sandbox requires shortcode {SANDBOX_SHORTCODE} (Safaricom's test till), "
                f"but yours is {shortcode}. A real Till number cannot be used in sandbox.")
        if passkey and passkey != SANDBOX_PASSKEY:
            wrong.append("The sandbox passkey is Safaricom's published test value, not your "
                         "production passkey.")
        if not passkey:
            wrong.append(f"Passkey is empty. For a sandbox test, use Safaricom's published "
                         f"sandbox passkey (ends ...{SANDBOX_PASSKEY[-8:]}).")
    if "production" in accepted and not passkey:
        wrong.append("Passkey is empty. Production needs the passkey issued for your own "
                     "shortcode on the Daraja portal.")

    print("\nTo finish the integration:")
    if wrong:
        for w in wrong:
            print(f"  - {w}")
    else:
        print("  - Credentials and shortcode are consistent.")
    print("  - Set server.public_base_url to a public https URL, or Safaricom cannot")
    print("    deliver the payment callback (use: cloudflared tunnel --url http://localhost:8090).")
    if "production" not in accepted:
        print("  - Sandbox never sends money to your Till; it only proves the flow works.")

    if args.apply_sandbox:
        if "sandbox" not in accepted:
            print("\nRefusing to apply sandbox settings: these keys are not sandbox keys.")
            sys.exit(1)
        m = cfg.data.setdefault("mpesa", {})
        before = {k: m.get(k) for k in ("shortcode", "passkey", "environment")}
        m["shortcode"] = SANDBOX_SHORTCODE
        m["passkey"] = SANDBOX_PASSKEY
        m["environment"] = "sandbox"
        save_config(cfg)
        print("\nApplied sandbox settings:")
        print(f"  shortcode   {before['shortcode']!r} -> {SANDBOX_SHORTCODE}")
        if before["shortcode"] not in (None, "", SANDBOX_SHORTCODE):
            print(f"  NOTE: {before['shortcode']} was looked like your real Till. It is needed for")
            print(f"        PRODUCTION only — keep it noted: {before['shortcode']}")
        print(f"  passkey     {'set' if before['passkey'] else 'empty'} -> Safaricom's published sandbox passkey")
        print(f"  environment {before['environment']!r} -> 'sandbox'")
        print("\nNext: turn off Test mode, then send a test STK push from the setup screen.")


if __name__ == "__main__":
    main()
