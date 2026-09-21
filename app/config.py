"""
Configuration loading.

Reads `config.json` from the project root (falls back to `config.example.json`
so the service can boot before you've filled anything in). Environment
variables override file values, which is how you keep secrets out of the file
in production:

    WIFI_MPESA_CONSUMER_KEY, WIFI_MPESA_CONSUMER_SECRET, WIFI_MPESA_PASSKEY,
    WIFI_MIKROTIK_PASSWORD, WIFI_ADMIN_PASSWORD
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

# env var name -> (dotted path into the config)
ENV_OVERRIDES = {
    "WIFI_MPESA_CONSUMER_KEY": "mpesa.consumer_key",
    "WIFI_MPESA_CONSUMER_SECRET": "mpesa.consumer_secret",
    "WIFI_MPESA_PASSKEY": "mpesa.passkey",
    "WIFI_MPESA_SHORTCODE": "mpesa.shortcode",
    "WIFI_MIKROTIK_PASSWORD": "mikrotik.password",
    "WIFI_ADMIN_PASSWORD": "server.admin_password",
    "WIFI_PUBLIC_BASE_URL": "server.public_base_url",
    "WIFI_SECRET_KEY": "server.secret_key",
}


def _set(data: dict, dotted: str, value: Any) -> None:
    node = data
    parts = dotted.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


class Config:
    def __init__(self, data: dict):
        self.data = data

    def __getitem__(self, dotted: str) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"Missing config key: {dotted}")
            node = node[part]
        return node

    def get(self, dotted: str, default: Any = None) -> Any:
        try:
            return self[dotted]
        except KeyError:
            return default

    @property
    def plans(self) -> list[dict]:
        return [p for p in self.data.get("plans", []) if p.get("active", True)]

    def plan(self, code: str) -> dict | None:
        for p in self.data.get("plans", []):
            if p["code"].lower() == str(code).lower():
                return p
        return None

    @property
    def db_path(self) -> Path:
        p = Path(self.data["database"]["path"])
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def _strip_comments(text: str) -> str:
    """JSON has no comment syntax, but humans add them anyway.

    Drop whole-line `#` or `//` comments so a stray note doesn't stop the service
    from booting. Inline trailing comments are deliberately NOT touched — a `#`
    inside a string (a password, say) must survive.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        kept.append(line)
    return "\n".join(kept)


def load_config() -> Config:
    source = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    raw = source.read_text(encoding="utf-8-sig")  # tolerate a BOM
    data = json.loads(_strip_comments(raw))
    for env_name, dotted in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value:
            _set(data, dotted, value)
    return Config(data)


def save_config(cfg: "Config") -> Path:
    """Write the config back to config.json, creating it from the example if needed.

    Used by the admin setup screen so the operator never has to hand-edit JSON.
    Written atomically: a crash mid-write cannot leave a corrupt config behind.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg.data, indent=2, ensure_ascii=False) + "\n"
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    return CONFIG_PATH


def set_path(cfg: "Config", dotted: str, value: Any) -> None:
    _set(cfg.data, dotted, value)
