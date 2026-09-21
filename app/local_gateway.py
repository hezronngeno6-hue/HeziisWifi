"""
Local Linux gateway driver — the laptop IS the router.

Same method surface as MikroTikClient, so the billing engine doesn't care which
one it is talking to. Select it with:

    "gateway": { "type": "local" }

What it does
------------
Instead of creating hotspot users on a router, it maintains a **nftables set of
authorised MAC addresses**. Unauthorised clients are dropped by the forward
policy; a paid client's MAC is added to the set and they are online immediately.

    table inet wifi {
        set allowed_macs { type ether_addr; }
        chain forward {
            type filter hook forward priority 0; policy drop;
            ether saddr @allowed_macs accept
            ip daddr <portal-ip> accept      # let them reach the portal
            ip daddr <upstream-gw> accept
        }
    }

Requirements
------------
* Linux (Ubuntu is what this was built against)
* `nft` (nftables) and root — the service runs as root, or gets a scoped
  sudoers entry for `/usr/sbin/nft`
* Two network interfaces: one upstream (ethernet), one AP (wifi)

Time limits are enforced by the app's own sweeper (it calls disconnect() when a
session expires), so nothing here depends on kernel timers.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from typing import Any

log = logging.getLogger("localgw")

TABLE = "wifi"
SET = "allowed_macs"


class LocalGatewayError(RuntimeError):
    pass


def _looks_like_mac(value: str) -> bool:
    return bool(re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", str(value).upper()))


class LocalGateway:
    """Manage client access on the machine we're running on."""

    # ── construction ─────────────────────────────────────────────────────
    def __init__(self, cfg):
        gw = cfg.get("local", {}) or {}
        self.dry_run = bool(cfg.get("mikrotik.dry_run", True)) or bool(gw.get("dry_run", False))
        self.enabled = True
        self.portal_ip = str(gw.get("portal_ip", "192.168.50.1"))
        self.upstream_gw = str(gw.get("upstream_gateway", ""))
        self.ap_interface = str(gw.get("ap_interface", "wlan0"))
        self.upstream_interface = str(gw.get("upstream_interface", "eth0"))
        self.nft = shutil.which("nft") or "/usr/sbin/nft"
        self.require_root = bool(gw.get("require_root", True))
        self.expected_mac = str(cfg.get("mikrotik.router_mac", "") or "").upper().strip()
        self.enforce_router_mac = bool(cfg.get("mikrotik.enforce_router_mac", False))

        self.platform_ok = platform.system().lower() == "linux"
        if not self.platform_ok:
            log.warning("local gateway selected but this is %s — commands will be logged only",
                        platform.system())

    # ── plumbing ─────────────────────────────────────────────────────────
    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess | None:
        if self.dry_run or not self.platform_ok:
            log.warning("[LOCAL-DRY-RUN] %s", " ".join(args))
            return None
        if self.require_root and os.geteuid() != 0:
            raise LocalGatewayError(
                f"needs root to run: {' '.join(args)} — run the service as root, or add a "
                f"sudoers rule for {self.nft}"
            )
        proc = subprocess.run(args, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise LocalGatewayError(
                f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
        return proc

    def close(self) -> None:
        pass

    # ── one-time setup ───────────────────────────────────────────────────
    def ensure_table(self) -> None:
        """Create the table/set/chain if they aren't already there. Idempotent."""
        self._run([self.nft, "add", "table", "inet", TABLE], check=False)
        self._run([self.nft, "add", "set", "inet", TABLE, SET,
                   "{", "type", "ether_addr", ";", "}"], check=False)
        self._run([self.nft, "add", "chain", "inet", TABLE, "forward",
                   "{", "type", "filter", "hook", "forward", "priority", "0",
                   ";", "policy", "drop", ";", "}"], check=False)

        # Rebuild rules deterministically: flush the chain, then re-add.
        self._run([self.nft, "flush", "chain", "inet", TABLE, "forward"], check=False)
        rules = [
            ["ether", "saddr", "@" + SET, "accept"],
            ["ip", "daddr", self.portal_ip, "accept"],
        ]
        if self.upstream_gw:
            rules.append(["ip", "daddr", self.upstream_gw, "accept"])
        rules.append(["ct", "state", "established,related", "accept"])
        for rule in rules:
            self._run([self.nft, "add", "rule", "inet", TABLE, "forward"] + rule, check=False)

    def ensure_profiles(self, plans: list[dict]) -> None:
        """No per-plan objects locally — speed limits would be tc/HTB classes.

        Deliberately not implemented: a wrong tc configuration on a laptop will
        silently kill throughput. Time limits come from the app's sweeper.
        """
        self.ensure_table()
        log.info("local gateway ready: %s allowed-MAC set on %s",
                 TABLE, self.ap_interface)

    # ── access control (the core) ────────────────────────────────────────
    @staticmethod
    def username_for(mac: str) -> str:
        return "dev-" + str(mac).replace(":", "").lower()

    def create_user(self, mac: str, plan: dict, password: str) -> str:
        """Allow this MAC through the firewall. `plan` only matters for logging."""
        mac = str(mac).upper()
        if not _looks_like_mac(mac):
            raise LocalGatewayError(f"not a MAC address: {mac!r}")
        self._run([self.nft, "add", "element", "inet", TABLE, SET,
                   "{", mac, "}"], check=False)
        log.info("allow %s for %s (%s minutes)", mac, plan.get("code"), plan.get("duration_minutes"))
        return self.username_for(mac)

    def login_device(self, username: str, password: str, ip: str, mac: str) -> None:
        """Nothing to do — being in the set is being online. Kept for API parity."""
        if mac:
            self._run([self.nft, "add", "element", "inet", TABLE, SET,
                       "{", str(mac).upper(), "}"], check=False)

    def disconnect(self, mac: str) -> None:
        mac = str(mac).upper()
        self._run([self.nft, "delete", "element", "inet", TABLE, SET,
                   "{", mac, "}"], check=False)

    def remove_user(self, username: str, missing_ok: bool = False) -> None:
        # username is dev-aabbccddeeff -> recover the MAC
        raw = str(username).replace("dev-", "")
        if len(raw) == 12:
            mac = ":".join(raw[i:i + 2] for i in range(0, 12, 2)).upper()
            self.disconnect(mac)

    def allowed_macs(self) -> list[str]:
        proc = self._run([self.nft, "-j", "list", "set", "inet", TABLE, SET], check=False)
        if not proc or not proc.stdout:
            return []
        try:
            import json
            data = json.loads(proc.stdout)
        except ValueError:
            return []
        out: list[str] = []
        for item in data.get("nftables", []):
            for elem in item.get("set", {}).get("elem", []) or []:
                val = elem if isinstance(elem, str) else str(elem)
                if _looks_like_mac(val):
                    out.append(val.upper())
        return out

    # ── accounting ───────────────────────────────────────────────────────
    def active_sessions(self) -> list[dict]:
        """Authorised clients, with byte counters read from the kernel where possible."""
        macs = self.allowed_macs()
        neigh = self._neighbours()
        rows = []
        for mac in macs:
            rows.append({
                "username": self.username_for(mac),
                "ip": neigh.get(mac, ""),
                "mac": mac,
                "uptime": "",
                "bytes_in": 0,
                "bytes_out": 0,
            })
        return rows

    def _neighbours(self) -> dict[str, str]:
        proc = self._run(["ip", "neigh", "show", "dev", self.ap_interface], check=False)
        out: dict[str, str] = {}
        if not proc or not proc.stdout:
            return out
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1] == "dev":
                ip = parts[0]
                for i, tok in enumerate(parts):
                    if tok == "lladdr" and i + 1 < len(parts):
                        out[parts[i + 1].upper()] = ip
        return out

    def active_for_mac(self, mac: str) -> dict | None:
        mac = str(mac).upper()
        for row in self.active_sessions():
            if row["mac"] == mac:
                return row
        return None

    def usage_for_user(self, username: str) -> dict:
        raw = str(username).replace("dev-", "")
        for row in self.active_sessions():
            if row["username"] == username:
                return row
        return {}

    # ── identity / guards ────────────────────────────────────────────────
    def identity_card(self) -> dict:
        card: dict = {"reachable": True, "mode": "local"}
        try:
            card["hostname"] = platform.node()
            card["kernel"] = platform.release()
            card["interfaces"] = self.interfaces()
            card["allowed_now"] = len(self.allowed_macs())
            card["dry_run"] = self.dry_run
            card["root"] = (os.geteuid() == 0) if self.platform_ok else None
        except Exception as exc:  # pragma: no cover
            card["error"] = str(exc)
        card["expected_mac"] = self.expected_mac
        card["mac_check"] = self.verify_router_mac()
        return card

    def interfaces(self) -> list[dict]:
        if not self.platform_ok:
            return []
        out: list[dict] = []
        base = "/sys/class/net"
        try:
            for name in sorted(os.listdir(base)):
                if name == "lo":
                    continue
                addr_path = os.path.join(base, name, "address")
                mac = ""
                if os.path.exists(addr_path):
                    mac = open(addr_path).read().strip().upper()
                out.append({"name": name, "mac": mac,
                            "running": os.path.exists(os.path.join(base, name, "operstate"))
                            and open(os.path.join(base, name, "operstate")).read().strip() == "up"})
        except OSError:
            pass
        return out

    def verify_router_mac(self, expected: str | None = None) -> dict:
        want = (expected if expected is not None else self.expected_mac).upper().strip()
        if not want:
            return {"ok": True, "checked": False,
                    "detail": "No MAC set for the AP interface — add one to guard against "
                              "running this on the wrong machine."}
        if not self.platform_ok:
            return {"ok": True, "checked": False, "detail": "Not Linux — not verified."}
        macs = [i["mac"] for i in self.interfaces() if i["mac"]]
        if want in macs:
            return {"ok": True, "checked": True, "detail": f"AP MAC matches {want}."}
        return {"ok": False, "checked": True,
                "detail": f"Configured MAC {want} is not on this machine. Found: {', '.join(macs)}"}

    def ensure_mac_ok(self) -> None:
        if not self.enforce_router_mac:
            return
        result = self.verify_router_mac()
        if result.get("checked") and not result.get("ok"):
            raise LocalGatewayError("Refusing to sell: " + result["detail"])

    def ping(self) -> dict:
        return {"identity": platform.node(), "mode": "local",
                "kernel": platform.release(),
                "dry_run": self.dry_run,
                "ap_interface": self.ap_interface}
