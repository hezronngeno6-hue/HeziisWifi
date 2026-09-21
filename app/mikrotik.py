"""
MikroTik RouterOS REST client (RouterOS v7+).

Why REST and not RADIUS
-----------------------
At 15 concurrent devices, RADIUS buys you nothing but another daemon to keep
alive. The portal can authorise a device directly against the router:

    1. a hotspot *user profile* per plan  -> speed limit + shared-users
    2. a hotspot *user* per device        -> uptime / data cap given to it
    3. `ip hotspot active login`          -> the device is online immediately

The router then does all the enforcement, which is exactly what you want: if
this service dies, existing sessions keep running.

Rate-limit format is RouterOS `rx/tx` = **upload/download**, so a plan with
speed_down_kbps=5000 and speed_up_kbps=2500 becomes `2500k/5000k`.

Enable the API on the router first:
    /ip service enable www-ssl        (or www for plain HTTP)
    /user add name=billing-api password=<secret> group=full
    /ip hotspot user profile add name=billing shared-users=3
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("mikrotik")


class MikroTikError(RuntimeError):
    pass


def _rate_limit(down_kbps: int, up_kbps: int) -> str | None:
    if not down_kbps and not up_kbps:
        return None
    down = f"{int(down_kbps)}k" if down_kbps else "0"
    up = f"{int(up_kbps)}k" if up_kbps else "0"
    return f"{up}/{down}"


def _uptime(duration_minutes: int) -> str:
    """RouterOS uptime syntax: 1d2h30m."""
    minutes = int(duration_minutes)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    out = ""
    if days:
        out += f"{days}d"
    if hours:
        out += f"{hours}h"
    if mins or not out:
        out += f"{mins}m"
    return out


def _bytes(total_mb: int) -> str | None:
    if not total_mb:
        return None
    return f"{int(total_mb)}M"


class MikroTikClient:
    """Thin wrapper over RouterOS REST. Set dry_run=True to log instead of call."""

    def __init__(self, cfg):
        self.enabled = bool(cfg.get("mikrotik.enabled", False))
        self.dry_run = bool(cfg.get("mikrotik.dry_run", False))
        self.host = cfg.get("mikrotik.host", "192.168.88.1")
        port = int(cfg.get("mikrotik.port", 443))
        self.use_tls = bool(cfg.get("mikrotik.use_tls", True))
        self.verify_tls = bool(cfg.get("mikrotik.verify_tls", False))
        self.username = cfg.get("mikrotik.username", "admin")
        self.password = cfg.get("mikrotik.password", "")
        self.hotspot_server = cfg.get("mikrotik.hotspot_server", "hotspot1")
        self.base_profile = cfg.get("mikrotik.hotspot_profile", "billing")
        # The router's own MAC, as typed in the setup screen. Lets the app refuse to
        # sell access on a router it wasn't configured for.
        self.expected_mac = str(cfg.get("mikrotik.router_mac", "") or "").upper().strip()
        self.enforce_router_mac = bool(cfg.get("mikrotik.enforce_router_mac", False))

        scheme = "https" if self.use_tls else "http"
        self.base = f"{scheme}://{self.host}:{port}/rest"
        self._client: httpx.Client | None = None

    # ── plumbing ─────────────────────────────────────────────────────────
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base,
                auth=(self.username, self.password),
                verify=self.verify_tls,
                timeout=httpx.Timeout(10.0),
                headers={"Content-Type": "application/json"},
            )
        return self._client

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self.enabled:
            log.debug("mikrotik disabled, skipping %s %s", method, path)
            return None
        if self.dry_run:
            log.warning("[DRY-RUN] %s %s %s", method, path, kwargs.get("json", ""))
            return {"dry_run": True, "path": path}
        try:
            resp = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MikroTikError(f"{method} {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise MikroTikError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ── health ───────────────────────────────────────────────────────────
    def ping(self) -> dict:
        """Returns router identity — used by the admin dashboard."""
        if self.dry_run:
            return {"dry_run": True, "identity": "(dry run)"}
        data = self._request("GET", "/system/identity")
        info = self._request("GET", "/system/resource")
        identity = data.get("name") if isinstance(data, dict) else None
        return {
            "identity": identity,
            "version": (info or {}).get("version") if isinstance(info, dict) else None,
            "uptime": (info or {}).get("uptime") if isinstance(info, dict) else None,
            "board": (info or {}).get("board-name") if isinstance(info, dict) else None,
        }

    # ── identity and MAC binding ─────────────────────────────────────────
    def interfaces(self) -> list[dict]:
        """Every interface that carries a MAC, which is how we prove it's the right box."""
        rows = self._request("GET", "/interface") or []
        if isinstance(rows, dict):
            rows = [rows]
        out = []
        for r in rows:
            mac = r.get("mac-address")
            if mac and not r.get("dry_run"):
                out.append({
                    "name": r.get("name"),
                    "mac": str(mac).upper(),
                    "running": r.get("running"),
                    "type": r.get("type"),
                })
        return out

    def router_macs(self) -> list[str]:
        return [i["mac"] for i in self.interfaces()]

    def verify_router_mac(self, expected: str | None = None) -> dict:
        """Is the router we're talking to the one the operator configured?"""
        want = (expected if expected is not None else self.expected_mac).upper().strip()
        if not want:
            return {"ok": True, "checked": False,
                    "detail": "No router MAC set — add one to guard against the wrong router."}
        if not self.enabled:
            return {"ok": True, "checked": False, "detail": "Router integration is off."}
        if self.dry_run:
            return {"ok": True, "checked": False,
                    "detail": f"Dry-run — {want} not verified against a live router."}
        try:
            macs = self.router_macs()
        except MikroTikError as exc:
            return {"ok": False, "checked": True, "detail": f"Cannot read router interfaces: {exc}"}
        if not macs:
            return {"ok": False, "checked": True,
                    "detail": "Router returned no interfaces with a MAC address."}
        if want in macs:
            return {"ok": True, "checked": True, "detail": f"Router MAC matches {want}."}
        return {"ok": False, "checked": True,
                "detail": f"Configured MAC {want} is not on this router. "
                          f"Interfaces found: {', '.join(macs)}"}

    def identity_card(self) -> dict:
        """Everything the setup screen needs to show about the router."""
        card: dict = {"reachable": False}
        try:
            ident = self._request("GET", "/system/identity") or {}
            res = self._request("GET", "/system/resource") or {}
            card.update({
                "reachable": True,
                "identity": ident.get("name") if isinstance(ident, dict) else None,
                "version": res.get("version") if isinstance(res, dict) else None,
                "board": res.get("board-name") if isinstance(res, dict) else None,
                "uptime": res.get("uptime") if isinstance(res, dict) else None,
                "arch": res.get("architecture-name") if isinstance(res, dict) else None,
            })
            try:
                rb = self._request("GET", "/system/routerboard") or {}
                if isinstance(rb, dict):
                    card["serial"] = rb.get("serial-number")
                    card["model"] = rb.get("model")
            except MikroTikError:
                pass  # not a RouterBOARD (e.g. CHR / x86) — fine
            card["interfaces"] = self.interfaces()
        except MikroTikError as exc:
            card["error"] = str(exc)
        if self.dry_run:
            card["dry_run"] = True
        card["expected_mac"] = self.expected_mac
        card["mac_check"] = self.verify_router_mac()
        return card

    def ensure_mac_ok(self) -> None:
        """Raise before any money is taken if the router isn't the configured one."""
        if not self.enforce_router_mac:
            return
        result = self.verify_router_mac()
        if result.get("checked") and not result.get("ok"):
            raise MikroTikError(
                "Refusing to sell: " + result["detail"]
                + " (mikrotik.enforce_router_mac is on)"
            )

    # ── profiles: one per plan, carries the speed limit ───────────────────
    def ensure_profile(self, profile: str, plan: dict) -> None:
        rate = _rate_limit(int(plan.get("speed_down_kbps", 0)), int(plan.get("speed_up_kbps", 0)))
        body: dict[str, Any] = {
            "name": profile,
            "shared-users": str(max(1, int(plan.get("devices", 1)))),
        }
        if rate:
            body["rate-limit"] = rate

        existing = self._request("GET", f"/ip/hotspot/user/profile/{profile}")
        if existing and isinstance(existing, dict) and not existing.get("dry_run"):
            self._request("PATCH", f"/ip/hotspot/user/profile/{profile}", json=body)
        else:
            self._request("PUT", "/ip/hotspot/user/profile", json=body)

    def ensure_profiles(self, plans: list[dict]) -> None:
        for plan in plans:
            self.ensure_profile(self.profile_for(plan["code"]), plan)

    @staticmethod
    def profile_for(plan_code: str) -> str:
        return f"plan-{str(plan_code).upper()}"

    # ── users: one per device per purchase ───────────────────────────────
    @staticmethod
    def username_for(mac: str) -> str:
        return "dev-" + str(mac).replace(":", "").lower()

    def create_user(self, mac: str, plan: dict, password: str) -> str:
        """Create (or replace) the hotspot user that represents this device's paid window."""
        username = self.username_for(mac)
        self.remove_user(username, missing_ok=True)

        body: dict[str, Any] = {
            "name": username,
            "password": password,
            "profile": self.profile_for(plan["code"]),
            "limit-uptime": _uptime(int(plan["duration_minutes"])),
            "comment": f"{plan['code']} {mac.upper()}",
        }
        data_cap = _bytes(int(plan.get("data_mb", 0)))
        if data_cap:
            body["limit-bytes-total"] = data_cap

        self._request("PUT", "/ip/hotspot/user", json=body)
        return username

    def remove_user(self, username: str, missing_ok: bool = False) -> None:
        try:
            self._request("DELETE", f"/ip/hotspot/user/{username}")
        except MikroTikError as exc:
            if not missing_ok:
                raise
            log.debug("remove_user(%s): %s", username, exc)

    def login_device(self, username: str, password: str, ip: str, mac: str) -> None:
        """Log the client in right away so they don't have to re-enter anything."""
        body = {"name": username, "password": password, "ip": ip, "mac-address": mac.upper()}
        self._request("POST", "/ip/hotspot/active/login", json=body)

    def disconnect(self, mac: str) -> None:
        """Kick any active session for this MAC."""
        rows = self._request("GET", "/ip/hotspot/active") or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if str(row.get("mac-address", "")).upper() == mac.upper():
                self._request("POST", "/ip/hotspot/active/remove",
                              json={".id": row[".id"]})

    # ── accounting ───────────────────────────────────────────────────────
    def active_sessions(self) -> list[dict]:
        rows = self._request("GET", "/ip/hotspot/active") or []
        if isinstance(rows, dict):
            rows = [rows]
        out = []
        for r in rows:
            if r.get("dry_run"):
                continue
            out.append({
                "username": r.get("user"),
                "ip": r.get("address"),
                "mac": str(r.get("mac-address", "")).upper(),
                "uptime": r.get("uptime"),
                "bytes_in": _to_int(r.get("bytes-in")),
                "bytes_out": _to_int(r.get("bytes-out")),
            })
        return out

    def active_for_mac(self, mac: str) -> dict | None:
        for row in self.active_sessions():
            if row["mac"] == mac.upper():
                return row
        return None

    def usage_for_user(self, username: str) -> dict:
        for row in self.active_sessions():
            if row["username"] == username:
                return row
        for row in (self._request("GET", "/ip/hotspot/user") or []):
            if isinstance(row, dict) and row.get("name") == username:
                return {
                    "username": username,
                    "uptime": row.get("uptime"),
                    "bytes_in": _to_int(row.get("bytes-in")),
                    "bytes_out": _to_int(row.get("bytes-out")),
                }
        return {}


def _to_int(value: Any) -> int:
    """RouterOS numbers come back as ints or strings like '12 345'."""
    if value is None:
        return 0
    try:
        return int(str(value).replace(" ", "").replace(",", ""))
    except ValueError:
        return 0
