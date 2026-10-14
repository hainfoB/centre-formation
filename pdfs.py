"""Certificate (attestation de formation) PDF, with a verification QR code."""
import io

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

NAVY, ORANGE, GREY = HexColor("#0b2545"), HexColor("#f97316"), HexColor("#5b6b82")

import os  # noqa: E402
import re  # noqa: E402

from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402

_FDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")
try:
    pdfmetrics.registerFont(TTFont("Taj", os.path.join(_FDIR, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("Taj-Bold", os.path.join(_FDIR, "DejaVuSans-Bold.ttf")))
    _HAS_AR = True
except Exception:  # noqa: BLE001
    _HAS_AR = False
_AR_RE = re.compile(r"[\u0600-\u06FF]")


def _txt(c, x, y, text, size, bold=False, align="center"):
    """Draw text; Arabic is shaped and reordered, and uses the Tajawal font."""
    text = text or ""
    font = ("Helvetica-Bold" if bold else "Helvetica")
    if _AR_RE.search(text) and _HAS_AR:
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            text = get_display(arabic_reshaper.reshape(text))
        except Exception:  # noqa: BLE001
            pass
        font = "Taj-Bold" if bold else "Taj"
    c.setFont(font, size)
    {"center": c.drawCentredString, "right": c.drawRightString}.get(align, c.drawString)(x, y, text)


def _qr(c, text, x, y, size):
    w = QrCodeWidget(text)
    b = w.getBounds()
    d = Drawing(size, size, transform=[size / (b[2] - b[0]), 0, 0, size / (b[3] - b[1]), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, x, y)


def _logo_reader(centre):
    import base64
    from reportlab.lib.utils import ImageReader
    uri = centre.get("logo") or ""
    if not uri.startswith("data:image/"):
        return None
    try:
        return ImageReader(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
    except Exception:  # noqa: BLE001
        return None


def certificate_pdf(centre, student, formation, enr, stats, verify_url):
    """One certificate (single page)."""
    return certificates_pdf(centre, [(student, formation, enr, stats, verify_url)])


def certificates_pdf(centre, items):
    """Several certificates in one PDF, one per page (items: student, formation, enr, stats, verify_url)."""
    buf = io.BytesIO()
    W, H = landscape(A4)
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle("Attestations" if len(items) > 1 else f"Attestation - {items[0][0].full_name}")
    logo = _logo_reader(centre)
    for student, formation, enr, stats, verify_url in items:
        _draw_certificate(c, W, H, centre, logo, student, formation, enr, stats, verify_url)
        c.showPage()
    c.save()
    return buf.getvalue()


def _draw_certificate(c, W, H, centre, logo, student, formation, enr, stats, verify_url):
    # frame
    c.setFillColor(white)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(NAVY)
    c.rect(0, H - 70, W, 70, fill=1, stroke=0)
    c.setFillColor(ORANGE)
    c.rect(0, H - 76, W, 6, fill=1, stroke=0)
    c.rect(0, 0, W, 10, fill=1, stroke=0)
    c.setStrokeColor(NAVY)
    c.setLineWidth(1.2)
    c.rect(24, 24, W - 48, H - 112, fill=0, stroke=1)
    # header
    x0 = 40
    if logo:
        c.setFillColor(white)
        c.roundRect(32, H - 64, 58, 58, 8, fill=1, stroke=0)
        c.drawImage(logo, 36, H - 60, 50, 50, preserveAspectRatio=True, mask="auto")
        x0 = 104
    c.setFillColor(white)
    _txt(c, x0, H - 42, centre.get("centre_name", ""), 20, True, "left")
    _txt(c, x0, H - 58, centre.get("centre_tagline", ""), 10, False, "left")
    if centre.get("centre_agrement"):
        c.drawRightString(W - 40, H - 42, "Agrément : " + centre["centre_agrement"])
    # title
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 34)
    c.drawCentredString(W / 2, H - 150, "ATTESTATION DE FORMATION")
    c.setFillColor(ORANGE)
    c.rect(W / 2 - 60, H - 172, 120, 3, fill=1, stroke=0)
    # body
    c.setFillColor(GREY)
    c.setFont("Helvetica", 13)
    c.drawCentredString(W / 2, H - 200, "Nous soussignés attestons que")
    c.setFillColor(NAVY)
    _txt(c, W / 2, H - 238, f"{student.last_name.upper()} {student.first_name}", 26, True)
    c.setFillColor(GREY)
    c.setFont("Helvetica", 12)
    born = ""
    if student.birth_date:
        born = "né(e) le " + student.birth_date.strftime("%d/%m/%Y") + (f" à {student.birth_place}" if student.birth_place else "")
    if born:
        _txt(c, W / 2, H - 260, born, 12)
    c.setFont("Helvetica", 13)
    c.drawCentredString(W / 2, H - 290, "a suivi avec assiduité la formation")
    c.setFillColor(NAVY)
    _txt(c, W / 2, H - 320, formation.title[:80], 19, True)
    c.setFillColor(GREY)
    c.setFont("Helvetica", 12)
    period = []
    if formation.starts_on:
        period.append("du " + formation.starts_on.strftime("%d/%m/%Y"))
    if formation.ends_on:
        period.append("au " + formation.ends_on.strftime("%d/%m/%Y"))
    hours = stats.get("hours") or formation.hours_total
    line = " ".join(period)
    if hours:
        line += f"  ·  volume horaire : {hours:g} h"
    line += f"  ·  assiduité : {stats.get('rate', 0)} %"
    c.drawCentredString(W / 2, H - 345, line.strip(" ·"))
    # footer
    c.setFont("Helvetica", 11)
    c.setFillColor(NAVY)
    place = re.split(r"[,،]", centre.get("centre_address") or "")[-1].strip()
    _txt(c, 60, 110, f"Fait à {place or '……………'}, le {enr.cert_issued_on.strftime('%d/%m/%Y')}", 11, False, "left")
    c.drawString(60, 92, f"N° {enr.cert_no}")
    c.drawRightString(W - 170, 110, "Le Directeur")
    if centre.get("director_name"):
        _txt(c, W - 170, 92, centre["director_name"], 11, True, "right")
    _qr(c, verify_url, W - 150, 40, 90)
    c.setFont("Helvetica", 7)
    c.setFillColor(GREY)
    c.drawCentredString(W - 105, 32, "Vérifier l'authenticité")
