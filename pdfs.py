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


# ── Student ID cards (CR80, 10 per A4) with QR code + Code128 barcode ────────────────────────────
def _shape(text, bold=False):
    text = text or ""
    font = "Helvetica-Bold" if bold else "Helvetica"
    if _AR_RE.search(text) and _HAS_AR:
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            text = get_display(arabic_reshaper.reshape(text))
        except Exception:  # noqa: BLE001
            pass
        font = "Taj-Bold" if bold else "Taj"
    return text, font


def _fit(c, text, x, y, maxw, size, minsize=6, bold=False, align="left", color=None):
    text, font = _shape(text, bold)
    while size > minsize and pdfmetrics.stringWidth(text, font, size) > maxw:
        size -= .5
    while pdfmetrics.stringWidth(text, font, size) > maxw and len(text) > 3:
        text = text[:-2]
    if color is not None:
        c.setFillColor(color)
    c.setFont(font, size)
    {"left": c.drawString, "center": c.drawCentredString, "right": c.drawRightString}[align](x, y, text)


def _draw_card(c, x, y, W, H, centre, logo, card):
    """x, y = bottom-left of the card (points)."""
    from reportlab.graphics.barcode import code128
    from reportlab.lib.units import mm
    c.saveState()
    c.setFillColor(white)
    c.setStrokeColor(HexColor("#cbd5e1"))
    c.setLineWidth(.4)
    c.roundRect(x, y, W, H, 3 * mm, fill=1, stroke=1)
    # header band
    hh = 13 * mm
    p = c.beginPath()
    p.roundRect(x, y + H - hh, W, hh, 3 * mm)
    c.clipPath(p, stroke=0, fill=0)
    c.setFillColor(NAVY)
    c.rect(x, y + H - hh, W, hh, fill=1, stroke=0)
    c.setFillColor(ORANGE)
    c.restoreState()
    c.saveState()
    c.setFillColor(ORANGE)
    c.rect(x, y + H - hh - 1.1 * mm, W, 1.1 * mm, fill=1, stroke=0)
    tx = x + 4 * mm
    if logo:
        try:
            c.drawImage(logo, x + 3 * mm, y + H - hh + 2 * mm, 9 * mm, 9 * mm, mask="auto", preserveAspectRatio=True)
            tx = x + 13 * mm
        except Exception:  # noqa: BLE001
            pass
    _fit(c, centre.get("centre_name") or "", tx, y + H - 6.2 * mm, W - (tx - x) - 3 * mm, 10, 6.5, True, color=white)
    _fit(c, card.get("subtitle") or "", tx, y + H - 10.4 * mm, W - (tx - x) - 3 * mm, 6.5, 5, False,
         color=HexColor("#fdba74"))
    # body: QR left, text right
    qs = 25 * mm
    _qr(c, card["code"], x + 4 * mm, y + H - hh - 3 * mm - qs, qs)
    bx = x + 4 * mm + qs + 4 * mm
    bw = W - (bx - x) - 4 * mm
    _fit(c, card["name"], bx, y + H - hh - 9 * mm, bw, 11, 6.5, True, color=NAVY)
    _fit(c, card.get("formation") or "", bx, y + H - hh - 14 * mm, bw, 7.5, 5.5, False, color=GREY)
    if card.get("extra"):
        _fit(c, card["extra"], bx, y + H - hh - 18 * mm, bw, 7, 5.5, False, color=GREY)
    # barcode strip
    bh = 8.5 * mm
    bc = code128.Code128(card["code"], barHeight=bh, barWidth=0.33 * mm, quiet=False, humanReadable=False)
    bwid = bc.width
    scale = min(1.0, (W - 10 * mm) / bwid)
    c.saveState()
    c.translate(x + (W - bwid * scale) / 2, y + 6 * mm)
    c.scale(scale, 1)
    c.setFillColor(HexColor("#000000"))
    bc.drawOn(c, 0, 0)
    c.restoreState()
    c.setFillColor(NAVY)
    c.setFont("Helvetica", 6.5)
    c.drawCentredString(x + W / 2, y + 2.4 * mm, " ".join([card["code"][:3], card["code"][3:6], card["code"][6:]])
                        if card["code"].isdigit() and len(card["code"]) == 9 else card["code"])
    c.restoreState()


def cards_pdf(centre, cards):
    """cards: list of dicts(code, name, formation, extra, subtitle). 10 per A4 page, with crop marks."""
    from reportlab.lib.units import mm
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Cartes")
    W, H = 85.6 * mm, 54 * mm
    gx, gy = 4 * mm, 2 * mm
    left = (A4[0] - 2 * W - gx) / 2
    top = A4[1] - (A4[1] - 5 * H - 4 * gy) / 2
    logo = _logo_reader(centre)
    for i, card in enumerate(cards):
        k = i % 10
        if i and k == 0:
            c.showPage()
        col, row = k % 2, k // 2
        _draw_card(c, left + col * (W + gx), top - (row + 1) * H - row * gy, W, H, centre, logo, card)
    c.save()
    return buf.getvalue()


# ── Company attendance report (émargement par entreprise / groupe) ────────────────────────────────
def attendance_report_pdf(centre, title, subtitle, blocks, generated):
    """blocks: [{"title", "meta", "seances": ["dd/mm"...], "rows": [{"name","sub","marks":[code...],"rate","hours"}]}]
    Codes: P présent, R retard, A absent, E excusé, "" non pointé. Long sessions are split in chunks of columns."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    buf = io.BytesIO()
    W, H = landscape(A4)
    logo = _logo_reader(centre)
    font = "Taj" if _HAS_AR else "Helvetica"
    fontb = "Taj-Bold" if _HAS_AR else "Helvetica-Bold"
    sh = lambda t: _shape(t)[0]  # noqa: E731

    def frame(c, doc):
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, H - 52, W, 52, fill=1, stroke=0)
        c.setFillColor(ORANGE)
        c.rect(0, H - 55, W, 3, fill=1, stroke=0)
        x = 24
        if logo:
            c.drawImage(logo, 22, H - 46, 38, 38, preserveAspectRatio=True, mask="auto")
            x = 70
        c.setFillColor(white)
        _fit(c, centre.get("centre_name", ""), x, H - 24, 380, 14, bold=True)
        _fit(c, title, x, H - 41, 520, 10)
        _fit(c, subtitle, W - 24, H - 24, 330, 10, align="right", bold=True)
        c.setFillColor(GREY)
        c.setFont(font, 7.5)
        c.drawString(24, 16, sh(f"Document généré le {generated} · P = présent, R = retard, A = absent, E = excusé"))
        c.drawRightString(W - 24, 16, f"{doc.page}")
        c.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=(W, H), leftMargin=24, rightMargin=24, topMargin=72, bottomMargin=34,
                            title=title)
    st_h = ParagraphStyle("h", fontName=fontb, fontSize=12, textColor=NAVY, spaceAfter=2)
    st_m = ParagraphStyle("m", fontName=font, fontSize=8.5, textColor=GREY, spaceAfter=6)
    story = []
    colors = {"P": HexColor("#dcfce7"), "R": HexColor("#fef3c7"), "A": HexColor("#fee2e2"), "E": HexColor("#e0e7ff")}
    if not blocks:
        story.append(Paragraph(sh("Aucune séance sur la période choisie."), st_h))
    for bi, b in enumerate(blocks):
        days = b["seances"]
        chunk = 16
        parts = [list(range(i, min(i + chunk, len(days)))) for i in range(0, max(len(days), 1), chunk)] or [[]]
        for pi, idx in enumerate(parts):
            head = [sh("Salarié"), sh("Fonction / structure")] + [days[i] for i in idx]
            last = pi == len(parts) - 1
            if last:
                head += [sh("Taux"), sh("Heures")]
            data = [head]
            for r in b["rows"]:
                line = [sh(r["name"]), sh(r.get("sub", ""))] + [r["marks"][i] for i in idx]
                if last:
                    line += [f'{r["rate"]} %', f'{r["hours"]}']
                data.append(line)
            namew, subw = 150, 130
            rest = W - 48 - namew - subw - (90 if last else 0)
            cw = [namew, subw] + [max(26, min(40, rest / max(len(idx), 1)))] * len(idx) + ([45, 45] if last else [])
            t = Table(data, colWidths=cw, repeatRows=1)
            style = [("FONT", (0, 0), (-1, -1), font, 8), ("FONT", (0, 0), (-1, 0), fontb, 8),
                     ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), white),
                     ("ALIGN", (2, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                     ("GRID", (0, 0), (-1, -1), .4, HexColor("#d6dce6")),
                     ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, HexColor("#f6f8fb")])]
            for ri, r in enumerate(b["rows"], 1):
                for ci, i in enumerate(idx):
                    code = r["marks"][i]
                    if code in colors:
                        style.append(("BACKGROUND", (2 + ci, ri), (2 + ci, ri), colors[code]))
                if last and r["rate"] < b.get("min", 0):
                    style.append(("TEXTCOLOR", (-2, ri), (-2, ri), HexColor("#dc2626")))
            t.setStyle(TableStyle(style))
            ttl = b["title"] + (f"  (suite {pi + 1}/{len(parts)})" if len(parts) > 1 else "")
            story.append(KeepTogether([Paragraph(sh(ttl), st_h), Paragraph(sh(b.get("meta", "")), st_m)]))
            story.append(t)
            story.append(Spacer(1, 14))
        if bi < len(blocks) - 1 and len(b["rows"]) > 18:
            story.append(PageBreak())
    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    return buf.getvalue()
