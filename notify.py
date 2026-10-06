"""Email sending (Brevo HTTPS API first — Railway blocks SMTP on small plans — then SMTP) and WhatsApp links."""
import json
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _brevo(to, subject, text, html, sender_name):
    sender = os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER")
    payload = {"sender": {"email": sender, "name": sender_name}, "to": [{"email": to}],
               "subject": subject, "textContent": text}
    if html:
        payload["htmlContent"] = html
    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=json.dumps(payload).encode(),
                                 headers={"api-key": os.environ["BREVO_API_KEY"], "Content-Type": "application/json",
                                          "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print(f"Brevo error for {to}: {e}")
        return False


def send_email(to, subject, text, html=None, sender_name="Centre de formation"):
    """Returns True when sent. Never raises: without configuration it only logs."""
    if not to:
        return False
    if os.environ.get("BREVO_API_KEY"):
        return _brevo(to, subject, text, html, sender_name)
    host, port = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_PORT")
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if not all([host, port, user, pw]):
        print(f"[email non configuré] {to} — {subject}")
        return False
    sender = os.environ.get("EMAIL_FROM", user)
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    msg["Subject"], msg["From"], msg["To"] = subject, f"{sender_name} <{sender}>", to
    try:
        if int(port) == 465:
            with smtplib.SMTP_SSL(host, int(port), timeout=15) as s:
                s.login(user, pw)
                s.sendmail(sender, [to], msg.as_string())
        else:
            with smtplib.SMTP(host, int(port), timeout=15) as s:
                s.starttls()
                s.login(user, pw)
                s.sendmail(sender, [to], msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001
        print(f"SMTP error for {to}: {e}")
        return False


def html_mail(centre, title, paragraphs, button=None):
    """Simple branded HTML email (orange / dark blue / white)."""
    body = "".join(f'<p style="margin:0 0 12px;line-height:1.6">{p}</p>' for p in paragraphs)
    btn = ""
    if button:
        btn = (f'<p style="margin:18px 0"><a href="{button[1]}" style="background:#f97316;color:#fff;'
               f'padding:12px 22px;border-radius:10px;text-decoration:none;font-weight:bold">{button[0]}</a></p>')
    return (f'<div style="font-family:Arial,sans-serif;background:#f5f7fb;padding:24px">'
            f'<div style="max-width:560px;margin:auto;background:#fff;border-radius:14px;overflow:hidden">'
            f'<div style="background:#0b2545;color:#fff;padding:16px 22px;font-weight:bold;font-size:17px">'
            f'{centre}<div style="height:3px;background:#f97316;margin-top:10px;width:60px"></div></div>'
            f'<div style="padding:22px;color:#0b2545"><h2 style="margin:0 0 14px;font-size:19px">{title}</h2>'
            f'{body}{btn}</div></div></div>')


def wa_link(phone, text):
    """wa.me link with an Algerian number normalised to 213…"""
    d = "".join(c for c in (phone or "") if c.isdigit())
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("0"):
        d = "213" + d[1:]
    if not d:
        return ""
    return f"https://wa.me/{d}?text={urllib.parse.quote(text)}"
