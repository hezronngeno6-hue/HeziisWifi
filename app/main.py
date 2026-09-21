"""
FastAPI application — captive portal, payment callbacks, admin dashboard.

Run it:
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8090

Routes
------
  GET  /                      captive portal (MikroTik passes ?mac=&ip=)
  GET  /api/device            is this device online, and until when?
  POST /api/pay               start an M-Pesa STK push for a plan
  GET  /api/payment/{id}      poll a payment (the portal does this every 2s)
  POST /mpesa/callback        Safaricom's callback (must be publicly reachable)
  POST /api/voucher           redeem a printed code
  POST /api/reconnect         re-log a device whose session is still live
  GET  /admin                 dashboard (HTTP Basic)
  GET  /healthz               liveness
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import __version__
from .billing import BillingError, BillingService
from .config import ROOT, load_config, save_config, set_path
from .db import Database
from .gateway import gateway_type, make_gateway
from .mpesa import MpesaClient, MpesaError, mask_phone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("app")

CFG = load_config()
DB = Database(CFG.db_path)
ROUTER = make_gateway(CFG)
MPESA = MpesaClient(CFG)
BILLING = BillingService(CFG, DB, ROUTER, MPESA)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

SWEEP_SECONDS = 20


def reload_runtime() -> None:
    """Rebuild every singleton from config.json (used after the setup screen saves)."""
    global CFG, DB, ROUTER, MPESA, BILLING
    try:
        ROUTER.close()
        MPESA.close()
    except Exception:
        pass
    CFG = load_config()
    DB = Database(CFG.db_path)
    ROUTER = make_gateway(CFG)
    MPESA = MpesaClient(CFG)
    BILLING = BillingService(CFG, DB, ROUTER, MPESA)
    DB.init()
    DB.seed_plans(CFG.data.get("plans", []))
    log.info("runtime reloaded — gateway %s at %s, %s %s, mock=%s",
             gateway_type(CFG), CFG.get("mikrotik.host"),
             MPESA.till_label, MPESA.shortcode, MPESA.mock)


async def _sweeper() -> None:
    while True:
        try:
            result = await asyncio.to_thread(BILLING.sweep)
            if result.get("expired"):
                log.info("swept %s expired session(s)", result["expired"])
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await asyncio.to_thread(BILLING.bootstrap)
    except Exception:
        log.exception("bootstrap failed — check config.json")
    task = asyncio.create_task(_sweeper())
    log.info("%s v%s listening — %s plans, gateway %s%s",
             CFG.get("site.name", "WiFi"), __version__, len(BILLING.plans()),
             gateway_type(CFG), " (DRY RUN)" if getattr(ROUTER, "dry_run", False) else "")
    yield
    task.cancel()
    ROUTER.close()
    MPESA.close()


app = FastAPI(title="WiFi Billing", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

basic = HTTPBasic(auto_error=False)


def require_admin(creds: HTTPBasicCredentials | None = Depends(basic)) -> str:
    expected = str(CFG.get("server.admin_password", ""))
    if not creds or not secrets.compare_digest(creds.password or "", expected):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Admin credentials required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username or "admin"


# ─────────────────────────────────────────────────────────────────────────────
# request bodies
# ─────────────────────────────────────────────────────────────────────────────
class PayRequest(BaseModel):
    mac: str = Field(min_length=5)
    plan_code: str = Field(min_length=1)
    phone: str = Field(min_length=9)
    ip: str | None = None


class VoucherRequest(BaseModel):
    mac: str
    code: str
    ip: str | None = None


class MacRequest(BaseModel):
    mac: str
    ip: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# captive portal
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def portal(request: Request,
                 mac: str | None = Query(default=None),
                 ip: str | None = Query(default=None)):
    mac = (mac or "").upper()
    ctx = {
        "request": request,
        "site": CFG.get("site.name", "WiFi"),
        "currency": CFG.get("site.currency", "KES"),
        "support_phone": CFG.get("site.support_phone", ""),
        "plans": BILLING.plans(),
        "mac": mac,
        "ip": ip or "",
        "mpesa_mock": MPESA.mock,
        "status": BILLING.status_for_device(mac) if mac else {"active": False},
    }
    return TEMPLATES.TemplateResponse("portal.html", ctx)


@app.get("/api/device")
async def api_device(mac: str):
    return BILLING.status_for_device(mac)


@app.post("/api/pay")
async def api_pay(body: PayRequest):
    try:
        if body.ip:
            DB.touch_device(body.mac.upper(), body.ip)
        return BILLING.start_payment(body.phone, body.plan_code, body.mac, body.ip)
    except (BillingError, MpesaError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # pragma: no cover
        log.exception("pay failed")
        raise HTTPException(status_code=500, detail=f"Payment failed to start: {exc}")


@app.get("/api/payment/{checkout_id}")
async def api_payment(checkout_id: str):
    return BILLING.payment_status(checkout_id)


@app.post("/api/voucher")
async def api_voucher(body: VoucherRequest):
    try:
        return {"ok": True, "session": BILLING.redeem_voucher(body.code, body.mac, body.ip)}
    except BillingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/reconnect")
async def api_reconnect(body: MacRequest):
    try:
        return {"ok": True, "session": BILLING.reconnect(body.mac, body.ip)}
    except BillingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/mpesa/callback")
async def mpesa_callback(payload: dict):
    """Safaricom retries this endpoint — always answer 200 once we've stored it."""
    try:
        result = await asyncio.to_thread(BILLING.handle_callback, payload)
        log.info("callback %s -> %s", result.checkout_request_id, result.result_desc)
    except Exception:
        log.exception("callback processing failed")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.get("/healthz")
async def healthz():
    router_ok, router_error = None, None
    try:
        router_ok = await asyncio.to_thread(ROUTER.ping)
    except Exception as exc:
        router_error = str(exc)
    return {
        "ok": True,
        "version": __version__,
        "router": router_ok,
        "router_error": router_error,
        "mpesa_mock": MPESA.mock,
        "active_sessions": BILLING.stats()["active_sessions"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# admin
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, _: str = Depends(require_admin)):
    return TEMPLATES.TemplateResponse("admin.html", {
        "request": request,
        "site": CFG.get("site.name", "WiFi"),
        "currency": CFG.get("site.currency", "KES"),
        "stats": BILLING.stats(),
        "sessions": BILLING.active_sessions(),
        "payments": BILLING.recent_payments(25),
        "audit": BILLING.recent_audit(30),
        "plans": BILLING.plans(),
        "destination": BILLING.destination(),
    })


@app.get("/admin/api/stats")
async def admin_stats(_: str = Depends(require_admin)):
    return {
        "stats": BILLING.stats(),
        "sessions": BILLING.active_sessions(),
        "payments": BILLING.recent_payments(25),
    }


@app.post("/admin/api/kick")
async def admin_kick(mac: str, _: str = Depends(require_admin)):
    BILLING.kick(mac)
    return {"ok": True}


@app.post("/admin/api/vouchers")
async def admin_vouchers(plan_code: str, count: int = 10, _: str = Depends(require_admin)):
    codes = BILLING.create_vouchers(plan_code, count)
    return {"ok": True, "count": len(codes), "codes": codes}


@app.get("/admin/vouchers.pdf")
async def admin_voucher_pdf(batch: str | None = None, count: int = 12,
                            plan_code: str | None = None,
                            _: str = Depends(require_admin)):
    """Create a fresh batch and return a printable PDF sheet."""
    plan = plan_code or (BILLING.plans()[0]["code"] if BILLING.plans() else "DAY1")
    rows = DB.q(
        """SELECT v.code, v.plan_code, p.name AS plan_name, p.price, p.duration_minutes
           FROM vouchers v JOIN plans p ON p.code = v.plan_code
           WHERE v.used_at IS NULL AND v.plan_code = ?
           ORDER BY v.created_at DESC LIMIT ?""",
        (plan, int(count)),
    )
    if len(rows) < count:
        BILLING.create_vouchers(plan, int(count) - len(rows), batch)
        rows = DB.q(
            """SELECT v.code, v.plan_code, p.name AS plan_name, p.price, p.duration_minutes
               FROM vouchers v JOIN plans p ON p.code = v.plan_code
               WHERE v.used_at IS NULL AND v.plan_code = ?
               ORDER BY v.created_at DESC LIMIT ?""",
            (plan, int(count)),
        )

    from tools.vouchers import build_voucher_pdf

    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"vouchers_{plan}_{len(rows)}.pdf"
    build_voucher_pdf([dict(r) for r in rows], target,
                      site_name=CFG.get("site.name", "WiFi"),
                      currency=CFG.get("site.currency", "KES"),
                      portal_url=CFG.get("server.public_base_url", ""))
    return Response(
        content=target.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{target.name}"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# setup screen — where the operator types in the router MAC and the till number
# ─────────────────────────────────────────────────────────────────────────────
SECRET_FIELDS = ("router_password", "mpesa_consumer_key",
                 "mpesa_consumer_secret", "mpesa_passkey")


def _form_bool(form, name: str) -> bool:
    return str(form.get(name, "")).lower() in ("1", "on", "true", "yes")


def _form_int(form, name: str, default: int) -> int:
    try:
        return int(str(form.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _current_settings() -> dict:
    """Config as the setup form sees it: secrets reported as present/absent only."""
    has = {
        "router_password": bool(str(CFG.get("mikrotik.password", "") or "").strip()),
        "mpesa_consumer_key": bool(str(CFG.get("mpesa.consumer_key", "") or "").strip()),
        "mpesa_consumer_secret": bool(str(CFG.get("mpesa.consumer_secret", "") or "").strip()),
        "mpesa_passkey": bool(str(CFG.get("mpesa.passkey", "") or "").strip()),
    }
    return {
        "site_name": CFG.get("site.name", ""),
        "currency": CFG.get("site.currency", "KES"),
        "support_phone": CFG.get("site.support_phone", ""),
        "public_base_url": CFG.get("server.public_base_url", ""),
        "router_host": CFG.get("mikrotik.host", ""),
        "router_port": CFG.get("mikrotik.port", 443),
        "router_use_tls": bool(CFG.get("mikrotik.use_tls", True)),
        "router_username": CFG.get("mikrotik.username", ""),
        "router_mac": CFG.get("mikrotik.router_mac", ""),
        "router_enforce_mac": bool(CFG.get("mikrotik.enforce_router_mac", False)),
        "router_dry_run": bool(CFG.get("mikrotik.dry_run", True)),
        "mpesa_till_type": MPESA.till_type,
        "mpesa_shortcode": CFG.get("mpesa.shortcode", ""),
        "mpesa_environment": CFG.get("mpesa.environment", "sandbox"),
        "mpesa_mock": bool(CFG.get("mpesa.mock", True)),
        "has_secrets": has,
    }


@app.get("/admin/setup", response_class=HTMLResponse)
async def admin_setup(request: Request, saved: str | None = None,
                      _: str = Depends(require_admin)):
    router_card = await asyncio.to_thread(ROUTER.identity_card)
    return TEMPLATES.TemplateResponse("setup.html", {
        "request": request,
        "site": CFG.get("site.name", "WiFi"),
        "currency": CFG.get("site.currency", "KES"),
        "s": _current_settings(),
        "destination": BILLING.destination(),
        "router_card": router_card,
        "saved": saved,
        "config_path": str(Path(ROOT) / "config.json"),
    })


@app.post("/admin/setup")
async def admin_setup_save(request: Request, _: str = Depends(require_admin)):
    form = await request.form()
    changes: list[str] = []

    def put(dotted: str, value) -> None:
        if CFG.get(dotted) != value:
            changes.append(dotted)
            set_path(CFG, dotted, value)

    put("site.name", str(form.get("site_name", "")).strip() or "WiFi")
    put("site.currency", str(form.get("currency", "KES")).strip() or "KES")
    put("site.support_phone", str(form.get("support_phone", "")).strip())
    put("server.public_base_url", str(form.get("public_base_url", "")).strip().rstrip("/"))

    put("mikrotik.host", str(form.get("router_host", "")).strip())
    put("mikrotik.port", _form_int(form, "router_port", 443))
    put("mikrotik.use_tls", _form_bool(form, "router_use_tls"))
    put("mikrotik.username", str(form.get("router_username", "")).strip())
    put("mikrotik.router_mac", str(form.get("router_mac", "")).strip().upper())
    put("mikrotik.enforce_router_mac", _form_bool(form, "router_enforce_mac"))
    put("mikrotik.dry_run", _form_bool(form, "router_dry_run"))

    put("mpesa.till_type", str(form.get("mpesa_till_type", "buy_goods")).strip().lower())
    # The dropdown must win. `transaction_type` is an escape hatch for hand-edited
    # config; if it lingers it silently overrides the operator's choice, so a user
    # picking "Buy Goods" would still send CustomerPayBillOnline.
    if CFG.get("mpesa.transaction_type"):
        put("mpesa.transaction_type", "")
    put("mpesa.shortcode", str(form.get("mpesa_shortcode", "")).strip())
    put("mpesa.environment", str(form.get("mpesa_environment", "sandbox")).strip().lower())
    put("mpesa.mock", _form_bool(form, "mpesa_mock"))

    # secrets: only overwrite when a new value is actually typed
    secret_map = {
        "router_password": "mikrotik.password",
        "mpesa_consumer_key": "mpesa.consumer_key",
        "mpesa_consumer_secret": "mpesa.consumer_secret",
        "mpesa_passkey": "mpesa.passkey",
    }
    for field, dotted in secret_map.items():
        value = str(form.get(field, "")).strip()
        if value:
            put(dotted, value)

    new_admin = str(form.get("admin_password", "")).strip()
    if new_admin:
        put("server.admin_password", new_admin)

    save_config(CFG)
    await asyncio.to_thread(reload_runtime)
    DB.audit("admin", "setup_saved", ", ".join(sorted(set(changes))) or "no changes")
    log.info("setup saved: %s", ", ".join(sorted(set(changes))) or "no changes")

    note = "no changes" if not changes else f"updated: {', '.join(sorted(set(changes)))}"
    return RedirectResponse(url=f"/admin/setup?saved={note}", status_code=303)


@app.post("/admin/api/reload")
async def admin_reload(_: str = Depends(require_admin)):
    """Re-read config.json without restarting the service.

    Handy after editing the file by hand, and required after the smoke test, which
    temporarily rewrites config.json and must put the running service back in step
    with the restored file.
    """
    await asyncio.to_thread(reload_runtime)
    return {
        "ok": True,
        "site": CFG.get("site.name"),
        "router": CFG.get("mikrotik.host"),
        "router_dry_run": bool(CFG.get("mikrotik.dry_run", True)),
        "mpesa_mock": MPESA.mock,
        "till": f"{MPESA.till_label} {MPESA.shortcode}",
        "transaction_type": MPESA.tx_type,
        "callback": f"{MPESA.public_base_url}{MPESA.callback_path}",
    }


@app.post("/admin/setup/test-router")
async def admin_test_router(_: str = Depends(require_admin)):
    card = await asyncio.to_thread(ROUTER.identity_card)
    return {"ok": bool(card.get("reachable")), "card": card}


@app.post("/admin/setup/test-mpesa")
async def admin_test_mpesa(request: Request, _: str = Depends(require_admin)):
    """Save first, then probe Safaricom with whatever is now in config.json."""
    result = await asyncio.to_thread(MPESA.test_credentials)
    result["destination"] = MPESA.destination_summary()
    result["problems"] = MPESA.validate()
    return result


@app.post("/admin/setup/test-charge")
async def admin_test_charge(request: Request, _: str = Depends(require_admin)):
    """Fire a real STK push at the operator's own phone to prove the till receives money."""
    form = await request.form()
    phone = str(form.get("test_phone", "")).strip()
    amount = float(form.get("test_amount", 1) or 1)
    if not phone:
        return JSONResponse({"ok": False, "detail": "Enter a phone number to send the test to."})
    try:
        push = await asyncio.to_thread(
            MPESA.initiate_stk_push, phone, amount, "TEST", "Till test")
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": str(exc)})
    return JSONResponse({"ok": True, "detail": f"STK push sent to {mask_phone(phone)} "
                                                 f"for KES {amount:g}.",
                         "checkout_request_id": push.checkout_request_id})
