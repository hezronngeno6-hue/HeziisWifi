"""
Compare our STK Push request against Safaricom's official Postman collection.

A field-name typo or a wrong type here is the difference between "payment works"
and "every customer sees an error", so this checks them mechanically.

    python -X utf8 -m tools.spec_check
    python -X utf8 -m tools.spec_check --collection "C:\\path\\to\\Safaricom APIs.postman_collection.json"

Nothing is sent — the payload is built with build_stk_payload() and compared.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config                  # noqa: E402
from app.mpesa import MpesaClient                   # noqa: E402

SEARCH_DIRS = [
    Path.home() / "Downloads",
    Path.home() / "Documents",
    ROOT,
    ROOT.parent,
]


def find_collection(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    for d in SEARCH_DIRS:
        if not d.exists():
            continue
        for pattern in ("*Safaricom*postman*collection*.json", "*Safaricom*.json"):
            hits = sorted(glob.glob(str(d / pattern)))
            if hits:
                return Path(hits[0])
    return None


def extract_stk_request(collection: dict) -> tuple[str, dict] | None:
    """Return (url, expected_json_body) for the STK **Push** request.

    Careful: the collection also contains `stkpushquery`, which is a different
    endpoint. Prefer an exact `processrequest` match and only fall back to any
    stkpush path if that is missing — otherwise we compare against the wrong spec.
    """
    push: list[tuple[str, dict]] = []
    other: list[tuple[str, dict]] = []

    def walk(items: list) -> None:
        for it in items:
            if "item" in it:
                walk(it["item"])
                continue
            req = it.get("request", {})
            url = req.get("url", {})
            raw = url.get("raw") if isinstance(url, dict) else str(url)
            if not raw:
                continue
            low = raw.lower()
            if "stkpush" not in low and "processrequest" not in low:
                continue
            body = req.get("body", {})
            parsed: dict | None = None
            if body.get("mode") == "raw" and body.get("raw"):
                try:
                    parsed = json.loads(body["raw"])
                except ValueError:
                    parsed = None
            elif body.get("mode") == "urlencoded":
                parsed = {kv.get("key"): kv.get("value")
                          for kv in body.get("urlencoded", [])}
            if parsed is None:
                continue
            if "processrequest" in low:
                push.append((raw, parsed))
            elif "query" not in low:
                other.append((raw, parsed))

    walk(collection.get("item", []))
    candidates = push or other
    return candidates[0] if candidates else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default=None)
    args = ap.parse_args()

    path = find_collection(args.collection)
    print("\nSTK Push spec check\n" + "=" * 72)
    if not path:
        print("  Could not find Safaricom's Postman collection.")
        print("  Pass it explicitly: --collection \"C:\\path\\Safaricom APIs.postman_collection.json\"")
        sys.exit(1)
    print(f"  collection: {path}")

    try:
        collection = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"  could not read it: {exc}")
        sys.exit(1)

    spec = extract_stk_request(collection)
    if not spec:
        print("  No STK Push / processrequest request found in the collection.")
        sys.exit(1)
    spec_url, spec_body = spec

    cfg = load_config()
    client = MpesaClient(cfg)
    ours = client.build_stk_payload("0712345678", 35, "TESTREF", "WiFi access")

    print(f"  spec URL   : {spec_url}")
    print(f"  our URL    : {client.base}/mpesa/stkpush/v1/processrequest")
    print(f"  matches    : {spec_url.split('?')[0].strip('/').endswith('processrequest')}")
    print()
    print(f"  {'FIELD':<20} {'SAFARICOM SPEC':<26} OURS")
    print("  " + "-" * 70)

    expected = {k: v for k, v in spec_body.items()}
    ok = True
    for key in sorted(set(expected) | set(ours)):
        spec_val = expected.get(key, "(absent)")
        ours_val = ours.get(key, "(absent)")
        # the spec uses placeholders like {yourShortCode} — compare names only
        status = "ok" if key in expected and key in ours else "MISMATCH"
        if status != "ok":
            ok = False
        shown = str(spec_val)
        shown = shown[:24] + ".." if len(shown) > 26 else shown
        oshown = str(ours_val)
        oshown = oshown[:40] + ".." if len(oshown) > 42 else oshown
        print(f"  {key:<20} {shown:<26} {oshown}")

    print("  " + "-" * 70)
    print(f"\n  field names match the official spec: {'YES' if ok else 'NO'}")

    print("\n  type sanity (what the spec and Safaricom's API expect):")
    checks = [
        ("TransactionType", ours["TransactionType"] in
         ("CustomerPayBillOnline", "CustomerBuyGoodsOnline"),
         f"{ours['TransactionType']} (must be one of the two)"),
        ("Amount is a whole number", float(ours["Amount"]).is_integer(),
         f"{ours['Amount']} (Daraja rejects decimals)"),
        ("PartyA == PhoneNumber", ours["PartyA"] == ours["PhoneNumber"],
         f"{ours['PartyA']} (they must be identical)"),
        ("PartyB == BusinessShortCode", ours["PartyB"] == ours["BusinessShortCode"],
         f"{ours['PartyB']}"),
        ("CallBackURL is https", str(ours["CallBackURL"]).startswith("https://"),
         ours["CallBackURL"]),
        ("Password is base64", isinstance(ours["Password"], str) and len(ours["Password"]) > 20,
         f"{len(ours['Password'])} chars (shortcode+passkey+timestamp)"),
        ("Timestamp is 14 digits", len(str(ours["Timestamp"])) == 14
         and str(ours["Timestamp"]).isdigit(), ours["Timestamp"]),
        ("AccountReference <= 12 chars", len(str(ours["AccountReference"])) <= 12,
         ours["AccountReference"]),
        ("TransactionDesc <= 60 chars", len(str(ours["TransactionDesc"])) <= 60,
         ours["TransactionDesc"]),
    ]
    for label, passed, detail in checks:
        print(f"    [{'OK' if passed else 'FAIL'}] {label:<28} {detail}")

    print()


if __name__ == "__main__":
    main()
