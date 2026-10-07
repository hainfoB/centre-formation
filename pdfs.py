"""Certificate (attestation de formation) PDF, with a verification QR code."""
import io

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

NAVY, ORANGE, GREY = HexColor("#0b2545"), HexColor("#f97316"), HexColor("#5b6b82")


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
    c.setFont("Helvetica-Bold", 20)
    c.drawString(x0, H - 42, centre.get("centre_name", ""))
    c.setFont("Helvetica", 10)
    c.drawString(x0, H - 58, centre.get("centre_tagline", ""))
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
    c.setFont("Helvetica-Bold", 26)
    c.drawCentredString(W / 2, H - 238, f"{student.last_name.upper()} {student.first_name}")
    c.setFillColor(GREY)
    c.setFont("Helvetica", 12)
    born = ""
    if student.birth_date:
        born = "né(e) le " + student.birth_date.strftime("%d/%m/%Y") + (f" à {student.birth_place}" if student.birth_place else "")
    if born:
        c.drawCentredString(W / 2, H - 260, born)
    c.setFont("Helvetica", 13)
    c.drawCentredString(W / 2, H - 290, "a suivi avec assiduité la formation")
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 19)
    c.drawCentredString(W / 2, H - 320, formation.title[:80])
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
    place = (centre.get("centre_address") or "").split(",")[-1].strip()
    c.drawString(60, 110, f"Fait à {place or '……………'}, le {enr.cert_issued_on.strftime('%d/%m/%Y')}")
    c.drawString(60, 92, f"N° {enr.cert_no}")
    c.drawRightString(W - 170, 110, "Le Directeur")
    if centre.get("director_name"):
        c.setFont("Helvetica-Bold", 11)
        c.drawRightString(W - 170, 92, centre["director_name"])
    _qr(c, verify_url, W - 150, 40, 90)
    c.setFont("Helvetica", 7)
    c.setFillColor(GREY)
    c.drawCentredString(W - 105, 32, "Vérifier l'authenticité")
