"""
SQLite data layer.

Single-file database, WAL mode, one connection per thread. At 15 concurrent
devices this is enormously over-specified — SQLite will happily do a few
thousand devices.

Tables
------
plans      catalogue of what you sell (seeded from config.json on boot)
devices    one row per MAC address ever seen
payments   one row per M-Pesa checkout attempt, with its own state machine
sessions   one row per paid access window granted to a device
vouchers   pre-printed codes (cash channel / resellers)
audit      append-only log of everything the system did
settings   small key/value store for runtime flags
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS plans (
    code             TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    price            REAL NOT NULL,
    duration_minutes INTEGER NOT NULL,
    data_mb          INTEGER NOT NULL DEFAULT 0,
    devices          INTEGER NOT NULL DEFAULT 1,
    speed_down_kbps  INTEGER NOT NULL DEFAULT 0,
    speed_up_kbps    INTEGER NOT NULL DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS devices (
    mac         TEXT PRIMARY KEY,
    ip          TEXT,
    label       TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_request_id TEXT,
    checkout_request_id TEXT UNIQUE,
    phone               TEXT NOT NULL,
    amount              REAL NOT NULL,
    plan_code           TEXT NOT NULL REFERENCES plans(code),
    device_mac          TEXT,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    result_code         INTEGER,
    result_desc         TEXT,
    mpesa_receipt       TEXT,
    raw_callback        TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_phone  ON payments(phone);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);

CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    mac            TEXT NOT NULL,
    ip             TEXT,
    plan_code      TEXT NOT NULL REFERENCES plans(code),
    payment_id     INTEGER REFERENCES payments(id),
    voucher_code   TEXT,
    mikrotik_user  TEXT,
    mikrotik_pass  TEXT,
    started_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    released_at    TEXT,
    bytes_in       INTEGER NOT NULL DEFAULT 0,
    bytes_out      INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'ACTIVE'
);
CREATE INDEX IF NOT EXISTS idx_sessions_mac    ON sessions(mac);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

CREATE TABLE IF NOT EXISTS vouchers (
    code             TEXT PRIMARY KEY,
    plan_code        TEXT NOT NULL REFERENCES plans(code),
    batch            TEXT,
    created_at       TEXT NOT NULL,
    used_at          TEXT,
    used_by_mac      TEXT,
    session_id       INTEGER REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    actor   TEXT,
    action  TEXT NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_local = threading.local()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    # ── connection handling ──────────────────────────────────────────────
    def connect(self) -> sqlite3.Connection:
        conn = getattr(_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 15000")
            _local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction. SQLite in autocommit mode + BEGIN IMMEDIATE."""
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ── setup ────────────────────────────────────────────────────────────
    def init(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        self._migrate(conn)
        self.audit("system", "db_init", f"schema ready at {self.path}")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the first release. Idempotent."""
        additions = {
            "payments": {
                "shortcode": "TEXT",      # which till/paybill this sale was raised on
                "till_type": "TEXT",      # 'buy_goods' or 'paybill'
            },
            "sessions": {
                "router_mac": "TEXT",     # router that granted the session
            },
        }
        for table, columns in additions.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def seed_plans(self, plans: Iterable[dict]) -> None:
        with self.tx() as conn:
            for p in plans:
                conn.execute(
                    """
                    INSERT INTO plans (code, name, price, duration_minutes, data_mb,
                                       devices, speed_down_kbps, speed_up_kbps, active)
                    VALUES (?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(code) DO UPDATE SET
                        name             = excluded.name,
                        price            = excluded.price,
                        duration_minutes = excluded.duration_minutes,
                        data_mb          = excluded.data_mb,
                        devices          = excluded.devices,
                        speed_down_kbps  = excluded.speed_down_kbps,
                        speed_up_kbps    = excluded.speed_up_kbps
                    """,
                    (p["code"], p["name"], float(p["price"]), int(p["duration_minutes"]),
                     int(p.get("data_mb", 0)), int(p.get("devices", 1)),
                     int(p.get("speed_down_kbps", 0)), int(p.get("speed_up_kbps", 0))),
                )

    # ── generic query helpers ────────────────────────────────────────────
    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def x(self, sql: str, params: tuple = ()) -> int:
        with self.tx() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    # ── domain helpers ───────────────────────────────────────────────────
    def audit(self, actor: str | None, action: str, detail: str = "") -> None:
        try:
            with self.tx() as conn:
                conn.execute(
                    "INSERT INTO audit (ts, actor, action, detail) VALUES (?,?,?,?)",
                    (now_iso(), actor, action, detail),
                )
        except Exception:
            pass  # auditing must never break a payment flow

    def touch_device(self, mac: str, ip: str | None = None, label: str | None = None) -> None:
        mac = mac.upper()
        ts = now_iso()
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO devices (mac, ip, label, first_seen, last_seen)
                VALUES (?,?,?,?,?)
                ON CONFLICT(mac) DO UPDATE SET
                    ip        = COALESCE(excluded.ip, devices.ip),
                    label     = COALESCE(excluded.label, devices.label),
                    last_seen = excluded.last_seen
                """,
                (mac, ip, label, ts, ts),
            )

    def active_session_for(self, mac: str) -> sqlite3.Row | None:
        return self.one(
            """SELECT * FROM sessions
               WHERE mac = ? AND status = 'ACTIVE' AND expires_at > ?
               ORDER BY expires_at DESC LIMIT 1""",
            (mac.upper(), now_iso()),
        )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
