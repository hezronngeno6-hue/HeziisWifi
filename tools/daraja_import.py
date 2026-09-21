"""
Daraja credential importer — fixes a pasted-in-the-wrong-box mistake.

The common failure: the whole credentials block (or the whole JSON response from
developer.safaricom.co.ke) gets pasted into the single "Consumer Key" field. This
tool reads that blob, works out what is inside it, and writes the individual
values into the right config.json fields.

    python -X utf8 -m tools.daraja_import            # inspect only, changes nothing
    python -X utf8 -m tools.daraja_import --apply     # write the corrected fields

Secrets are NEVER printed — only labels, lengths and shapes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import CONFIG_PATH, load_config, save_config   # noqa: E402

# field name -> regexes that would find it inside a pasted blob
PATTERNS = {
    "consumer_key": [r'"?consumer[_\s-]?key"?\s*[:=]\s*"?([A-Za-z0-9]{16,80})"?'],
    "consumer_secret": [r'"?consumer[_\s-]?secret"?\s*[:=]\s*"?([A-Za-z0-9]{16,80})"?'],
    "passkey": [r'"?pass[_\s-]?key"?\s*[:=]\s*"?([A-Za-z0-9]{16,90})"?'],
    "shortcode": [r'"?short[_\s-]?code"?\s*[:=]\s*"?(\d{5,7})"?',
                  r'"?business[_\s-]?short[_\s-]?code"?\s*[:=]\s*"?(\d{5,7})"?'],
}

ALNUM = re.compile(r"[A-Za-z0-9]{16,90}")


def describe(value: str) -> dict:
    """Structural summary of a value — never the value itself."""
    v = value or ""
    return {
        "length": len(v),
        "lines": v.count("\n") + 1 if v else 0,
        "whitespace_inside": bool(re.search(r"\s", v)),
        "is_json_like": v.strip().startswith(("{", "[")),
        "has_labels": bool(re.search(r"(?i)consumer|passkey|pass\s?key|secret", v)),
        "alnum_runs": len(ALNUM.findall(v)),
    }


def extract(blob: str) -> dict[str, str]:
    """Pull individual credential values out of a pasted blob."""
    found: dict[str, str] = {}
    text = blob or ""

    # 1. structured JSON, however mangled
    stripped = text.strip().strip(",")
    if stripped.startswith("{"):
        candidate = stripped if stripped.endswith("}") else stripped + "}"
        try:
            data = json.loads(candidate)
            flat = {}
            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    for k, v in node.items():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                        else:
                            flat[str(k).lower().replace(" ", "").replace("_", "")] = str(v)
                elif isinstance(node, list):
                    stack.extend(node)
            for field, aliases in {
                "consumer_key": ("consumerkey", "key"),
                "consumer_secret": ("consumersecret", "secret"),
                "passkey": ("passkey", "lipanampesapasskey"),
                "shortcode": ("shortcode", "businessshortcode"),
            }.items():
                for alias in aliases:
                    if alias in flat and ALNUM.fullmatch(flat[alias] or "") is not None:
                        found[field] = flat[alias]
                        break
                    if alias in flat and flat[alias].isdigit():
                        found[field] = flat[alias]
                        break
        except (ValueError, TypeError):
            pass

    # 2. labelled text (any order/format)
    for field, patterns in PATTERNS.items():
        if field in found:
            continue
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                found[field] = m.group(1)
                break

    # 3. nothing labelled — fall back to positional alphanumeric runs
    if "consumer_key" not in found:
        runs = ALNUM.findall(text)
        if runs:
            found["consumer_key"] = runs[0]
            if len(runs) > 1 and "consumer_secret" not in found:
                found["consumer_secret"] = runs[1]
            if len(runs) > 2 and "passkey" not in found:
                found["passkey"] = runs[2]

    return found


def main() -> None:
    ap = argparse.ArgumentParser(description="Repair pasted Daraja credentials.")
    ap.add_argument("--apply", action="store_true", help="write the corrected values")
    args = ap.parse_args()

    cfg = load_config()
    blob = str(cfg.get("mpesa.consumer_key", "") or "")
    secret = str(cfg.get("mpesa.consumer_secret", "") or "")
    passkey = str(cfg.get("mpesa.passkey", "") or "")

    print(f"\nconfig: {CONFIG_PATH}\n")
    print("current state (values hidden):")
    for label, value in (("consumer_key", blob), ("consumer_secret", secret), ("passkey", passkey)):
        d = describe(value)
        print(f"  {label:16} len={d['length']:<4} lines={d['lines']:<3} "
              f"json={str(d['is_json_like']):<5} labels={str(d['has_labels']):<5} "
              f"alnum_runs={d['alnum_runs']}")
    print(f"  shortcode        {cfg.get('mpesa.shortcode')}")
    print(f"  till_type        {cfg.get('mpesa.till_type')}")
    print(f"  environment      {cfg.get('mpesa.environment')}")

    if len(blob) <= 120 and not describe(blob)["has_labels"]:
        print("\nThe Consumer Key field already looks like a single key — nothing to repair.")
        return

    print("\nThe Consumer Key field holds more than one value. Extracting…")
    found = extract(blob)
    if not found:
        print("  Could not isolate any credentials from it. Re-copy them one field at a time.")
        sys.exit(1)

    for field, value in found.items():
        print(f"  found {field:16} len={len(value)}")

    if not args.apply:
        print("\nDry inspection only. Re-run with --apply to write these into config.json.")
        return

    setter = {
        "consumer_key": "mpesa.consumer_key",
        "consumer_secret": "mpesa.consumer_secret",
        "passkey": "mpesa.passkey",
        "shortcode": "mpesa.shortcode",
    }
    changed = []
    for field, value in found.items():
        dotted = setter[field]
        if field == "shortcode":
            current = str(cfg.get(dotted, "") or "")
            # only adopt a shortcode from the blob if we don't already have a real one
            if current and current not in ("174379", ""):
                continue
        if str(cfg.get(dotted, "") or "") != value:
            cfg.data.setdefault("mpesa", {})[dotted.split(".")[1]] = value
            changed.append(dotted)

    save_config(cfg)
    print(f"\nWrote: {', '.join(changed) if changed else 'nothing to change'}")
    print("Run `python -X utf8 -m tools.preflight` to re-check.")


if __name__ == "__main__":
    main()
