"""
Gateway factory — pick where customer access is enforced.

    "gateway": { "type": "mikrotik" }   a real MikroTik router over RouterOS REST
    "gateway": { "type": "local" }      this machine (Linux, nftables) is the router
    "gateway": { "type": "dry" }        log every command, enforce nothing

Both drivers expose the same methods (create_user / login_device / disconnect /
active_sessions / ensure_mac_ok ...), so the billing engine never knows which one
it is driving. `dry` is the local driver in dry-run mode, useful on Windows or for
a rehearsal.
"""

from __future__ import annotations

from .local_gateway import LocalGateway, LocalGatewayError
from .mikrotik import MikroTikClient, MikroTikError

# Everything the billing engine catches when a gateway operation fails.
GATEWAY_ERRORS = (MikroTikError, LocalGatewayError)

GATEWAY_TYPES = ("mikrotik", "local", "dry")


def gateway_type(cfg) -> str:
    kind = str(cfg.get("gateway.type", "") or "").strip().lower()
    if kind in GATEWAY_TYPES:
        return kind
    # Back-compat: no gateway section means the original MikroTik behaviour.
    return "mikrotik"


def make_gateway(cfg):
    """Build the configured gateway driver."""
    kind = gateway_type(cfg)
    if kind == "local":
        return LocalGateway(cfg)
    if kind == "dry":
        cfg.data.setdefault("mikrotik", {})["dry_run"] = True
        return LocalGateway(cfg)
    return MikroTikClient(cfg)
