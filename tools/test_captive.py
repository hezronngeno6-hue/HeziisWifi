"""
Regression test for the captive-portal probes.

A phone only shows "Sign in to network" if it gets the *wrong* answer to its
probe. Returning 204 for /generate_204 is the classic mistake — it tells Android
the network has internet, so the portal never appears and the customer just sees
"I'm connected" with no way to pay.

Run:  python -X utf8 -m tools.test_captive
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("HEZIIS_TEST", "1")

from starlette.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

# NOTE: TestClient is deliberately NOT used as a context manager, so the app's
# lifespan (which starts the sweeper and contacts the router) never runs.

PROBES = (
    "/generate_204",                # Android
    "/gen_204",                     # Android (older)
    "/mobile/status.php",           # Android (older)
    "/hotspot-detect.html",         # Apple
    "/library/test/success.html",   # Apple
    "/success.txt",                 # Firefox
    "/ncsi.txt",                    # Windows
    "/connecttest.txt",             # Windows
    "/redirect",                    # Windows
    "/kindle-wifi/wifistub.html",   # Kindle
)

TYPED_URLS = ("/facebook.com", "/some/deep/page", "/wp-login.php", "/a/b/c?d=e")

# path -> status a real client must still get
REAL_ROUTES = (
    ("/", 200),
    ("/healthz", 200),
    ("/docs", 200),
    ("/openapi.json", 200),
    ("/admin", 401),
    ("/favicon.ico", 404),
)


def main() -> int:
    c = TestClient(app, follow_redirects=False)
    failures: list[str] = []

    print("=" * 70)
    print("captive-portal probes  (must be 302 — NEVER 204)")
    print("=" * 70)
    for path in PROBES:
        r = c.get(path)
        loc = r.headers.get("location", "")
        if r.status_code == 204:
            note = "204 = phone thinks it IS online, portal never shows"
            failures.append(f"{path} returned 204")
        elif r.status_code == 302 and loc == "/":
            note = "-> portal"
        else:
            note = f"unexpected (location={loc!r})"
            failures.append(f"{path} returned {r.status_code}")
        flag = "ok " if not failures or failures[-1].split()[0] != path else "BAD"
        print(f"  {flag} {path:<30} {r.status_code}  {note}")

    print()
    print("=" * 70)
    print("any typed URL should land on the portal")
    print("=" * 70)
    for path in TYPED_URLS:
        r = c.get(path)
        ok = r.status_code == 302 and r.headers.get("location") == "/"
        if not ok:
            failures.append(f"{path} returned {r.status_code}")
        print(f"  {'ok ' if ok else 'BAD'} {path:<30} {r.status_code}")

    print()
    print("=" * 70)
    print("real routes must NOT be swallowed by the catch-all")
    print("=" * 70)
    for path, want in REAL_ROUTES:
        r = c.get(path)
        ok = r.status_code == want
        if not ok:
            failures.append(f"{path} returned {r.status_code}, wanted {want}")
        print(f"  {'ok ' if ok else 'BAD'} {path:<30} {r.status_code}  (want {want})")

    print()
    print("=" * 70)
    print("API endpoints survived")
    print("=" * 70)
    r = c.get("/api/device", params={"mac": "AA:BB:CC:DD:EE:FF"})
    ok = r.status_code == 200
    if not ok:
        failures.append(f"/api/device returned {r.status_code}")
    print(f"  {'ok ' if ok else 'BAD'} /api/device                    {r.status_code}")
    if ok:
        print(f"       {r.json()}")

    print()
    print("=" * 70)
    if failures:
        print(f"FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"ALL PASSED — {len(PROBES)} probes, {len(TYPED_URLS)} typed URLs, "
          f"{len(REAL_ROUTES)} real routes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
