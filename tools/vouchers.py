"""
Voucher sheet printer.

Generates an A4 PDF of scannable voucher cards — 12 per page (3 x 4). Each card
carries the code in large type plus a QR code that opens the portal with the
code pre-filled, so a customer can just point their camera at it.

    python -m tools.vouchers --plan DAY1 --count 12
    python -m tools.vouchers --plan HOUR1 --count 24 --out output/hour-vouchers.pdf
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAGE_W, PAGE_H = A4
MARGIN = 26
COLS, ROWS = 3, 4
PER_PAGE = COLS * ROWS

INK = (0.12, 0.16, 0.15)
BRAND = (0.055, 0.478, 0.373)
MUTED = (0.40, 0.45, 0.43)
CUT = (0.78, 0.82, 0.80)


def _qr_image(payload: str) -> ImageReader:
    qr = qrcode.QRCode(version=4, box_size=10, border=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0E7A5F", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def _fit_font(c: canvas.Canvas, text: str, font: str, max_size: float,
              max_width: float, min_size: float = 7.0) -> float:
    """Largest font size at which `text` still fits `max_width`.

    Codes are fixed-length here, but this keeps the card honest if you ever
    change the length or the site name gets long.
    """
    size = max_size
    while size > min_size and c.stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def _card(c: canvas.Canvas, x: float, y: float, w: float, h: float,
          row: dict, site_name: str, currency: str, portal_url: str) -> None:
    c.setStrokeColorRGB(*CUT)
    c.setDash(2, 2)
    c.setLineWidth(0.6)
    c.roundRect(x, y, w, h, 7, stroke=1, fill=0)
    c.setDash()

    pad = 11
    inner_w = w - 2 * pad
    top = y + h

    # ── band 1: identity and price ───────────────────────────────────────
    c.setFillColorRGB(*BRAND)
    c.setFont("Helvetica-Bold", _fit_font(c, site_name.upper(), "Helvetica-Bold", 10, inner_w))
    c.drawString(x + pad, top - 18, site_name.upper())

    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawString(x + pad, top - 28, "WIFI ACCESS VOUCHER")

    plan_name = str(row.get("plan_name", ""))
    c.setFillColorRGB(*INK)
    c.setFont("Helvetica-Bold", _fit_font(c, plan_name, "Helvetica-Bold", 15, inner_w))
    c.drawString(x + pad, top - 50, plan_name)

    minutes = int(row.get("duration_minutes", 0))
    human = (f"{minutes // 1440} day(s)" if minutes >= 1440
             else f"{minutes // 60} hour(s)" if minutes >= 60 else f"{minutes} min")
    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica", 8.5)
    c.drawString(x + pad, top - 62, human)

    c.setFillColorRGB(*BRAND)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(x + pad, top - 84, f"{currency} {float(row.get('price', 0)):g}")

    c.setStrokeColorRGB(*CUT)
    c.setLineWidth(0.5)
    c.line(x + pad, top - 94, x + w - pad, top - 94)

    # ── band 2: the code, on its own line, full width ────────────────────
    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(x + pad, top - 104, "VOUCHER CODE")

    code = str(row.get("code", ""))
    c.setFillColorRGB(*INK)
    c.setFont("Courier-Bold", _fit_font(c, code, "Courier-Bold", 21, inner_w))
    c.drawString(x + pad, top - 126, code)

    # ── band 3: QR bottom-right, instructions bottom-left ────────────────
    qr_side = 52
    qr = _qr_image(f"{portal_url.rstrip('/')}/?voucher={code}" if portal_url else code)
    c.drawImage(qr, x + w - pad - qr_side, top - 184, qr_side, qr_side, mask="auto")

    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica", 6.8)
    c.drawString(x + pad, top - 150, "1. Connect to the WiFi")
    c.drawString(x + pad, top - 160, "2. Scan the QR code")
    c.setFont("Helvetica-Oblique", 6.2)
    c.drawString(x + pad, top - 176, "One device.")
    c.drawString(x + pad, top - 184, "Not transferable.")


def build_voucher_pdf(rows: list[dict], out_path: Path,
                      site_name: str = "WiFi",
                      currency: str = "KES",
                      portal_url: str = "") -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(out_path), pagesize=A4)
    c.setTitle(f"{site_name} vouchers")

    cell_w = (PAGE_W - 2 * MARGIN) / COLS
    cell_h = (PAGE_H - 2 * MARGIN) / ROWS

    for index, row in enumerate(rows):
        slot = index % PER_PAGE
        if index and slot == 0:
            c.showPage()

        col = slot % COLS
        row_i = slot // COLS
        x = MARGIN + col * cell_w
        y = PAGE_H - MARGIN - (row_i + 1) * cell_h

        _card(c, x + 4, y + 4, cell_w - 8, cell_h - 8,
              row, site_name, currency, portal_url)

    c.save()
    return out_path


def _reveal(path: Path) -> None:
    """Tag the finished PDF in Windows Explorer."""
    try:
        subprocess.Popen(["explorer", "/select,", str(path.resolve())])
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Print a sheet of WiFi vouchers.")
    ap.add_argument("--plan", default=None, help="plan code, e.g. DAY1")
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--out", default=None)
    ap.add_argument("--create", action="store_true",
                    help="create this many new vouchers in the database first")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    from app.billing import BillingService
    from app.config import load_config
    from app.db import Database
    from app.mikrotik import MikroTikClient
    from app.mpesa import MpesaClient

    cfg = load_config()
    db = Database(cfg.db_path)
    db.init()
    db.seed_plans(cfg.data.get("plans", []))
    billing = BillingService(cfg, db, MikroTikClient(cfg), MpesaClient(cfg))

    plan_code = (args.plan or billing.plans()[0]["code"]).upper()

    if args.create or not db.q("SELECT 1 FROM vouchers WHERE used_at IS NULL AND plan_code = ? LIMIT 1",
                               (plan_code,)):
        new_codes = billing.create_vouchers(plan_code, args.count)
        print(f"Created {len(new_codes)} voucher(s) for {plan_code}")

    rows = db.q(
        """SELECT v.code, v.plan_code, p.name AS plan_name, p.price, p.duration_minutes
           FROM vouchers v JOIN plans p ON p.code = v.plan_code
           WHERE v.used_at IS NULL AND v.plan_code = ?
           ORDER BY v.created_at DESC LIMIT ?""",
        (plan_code, args.count),
    )

    out = Path(args.out) if args.out else ROOT / "output" / f"vouchers_{plan_code}_{len(rows)}.pdf"
    build_voucher_pdf([dict(r) for r in rows], out,
                      site_name=cfg.get("site.name", "WiFi"),
                      currency=cfg.get("site.currency", "KES"),
                      portal_url=cfg.get("server.public_base_url", ""))
    print(f"PDF  -> {out}")

    if not args.no_open:
        _reveal(out)


if __name__ == "__main__":
    main()
