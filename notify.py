"""Email sending (Brevo HTTPS API first — Railway blocks SMTP on small plans — then SMTP) and WhatsApp links."""
import base64
import json
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr


LAST_ERROR = {"msg": ""}
RESOLVER = None     # set by the app: () -> config dict of the current school (or platform)


def _env_cfg():
    if os.environ.get("BREVO_API_KEY"):
        return {"mode": "brevo", "api_key": os.environ["BREVO_API_KEY"],
                "from_email": os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER") or ""}
    if os.environ.get("SMTP_HOST"):
        return {"mode": "smtp", "host": os.environ.get("SMTP_HOST"), "port": os.environ.get("SMTP_PORT") or "587",
                "user": os.environ.get("SMTP_USER"), "password": os.environ.get("SMTP_PASSWORD"),
                "from_email": os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER") or ""}
    return None


def current_cfg():
    cfg = None
    if RESOLVER:
        try:
            cfg = RESOLVER()
        except Exception as e:  # noqa: BLE001
            print("mail cfg error", e)
    return cfg or _env_cfg()


def _brevo(cfg, to_list, subject, text, html, sender_name, attachments):
    payload = {"sender": {"email": cfg.get("from_email"), "name": cfg.get("from_name") or sender_name},
               "to": [{"email": t} for t in to_list], "subject": subject, "textContent": text or " "}
    if html:
        payload["htmlContent"] = html
    if cfg.get("reply_to"):
        payload["replyTo"] = {"email": cfg["reply_to"]}
    if attachments:
        payload["attachment"] = [{"name": n, "content": base64.b64encode(b).decode()} for n, b, _m in attachments]
    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=json.dumps(payload).encode(),
                                 headers={"api-key": cfg.get("api_key") or "", "Content-Type": "application/json",
                                          "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        LAST_ERROR["msg"] = f"Brevo {e.code} : {e.read()[:200].decode(errors='ignore')}"
    except Exception as e:  # noqa: BLE001
        LAST_ERROR["msg"] = f"Brevo : {e}"
    return False


def _smtp(cfg, to_list, subject, text, html, sender_name, attachments):
    host, port = cfg.get("host"), int(cfg.get("port") or 587)
    user, pw = cfg.get("user"), cfg.get("password")
    sender = cfg.get("from_email") or user
    msg = MIMEMultipart("mixed")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text or " ", "plain", "utf-8"))
    if html:
        alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)
    for n, b, mime in attachments or []:
        part = MIMEApplication(b, _subtype=(mime or "application/octet-stream").split("/")[-1])
        part.add_header("Content-Disposition", "attachment", filename=n)
        msg.attach(part)
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.get("from_name") or sender_name, sender))
    msg["To"] = ", ".join(to_list)
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    try:
        if port == 465 or cfg.get("security") == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                if user:
                    s.login(user, pw)
                s.sendmail(sender, to_list, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                if cfg.get("security") != "none":
                    s.starttls()
                    s.ehlo()
                if user:
                    s.login(user, pw)
                s.sendmail(sender, to_list, msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001
        LAST_ERROR["msg"] = f"SMTP : {e}"
        return False


def send_email(to, subject, text, html=None, sender_name="Centre de formation", attachments=None, cfg=None):
    """Returns True when sent. Never raises; the reason of a failure is in LAST_ERROR["msg"].
    to: one address or a list. attachments: [(filename, bytes, mime)]."""
    LAST_ERROR["msg"] = ""
    to_list = [t for t in ([to] if isinstance(to, str) else (to or [])) if t]
    if not to_list:
        LAST_ERROR["msg"] = "Aucun destinataire"
        return False
    cfg = cfg or current_cfg()
    if not cfg or not cfg.get("from_email"):
        LAST_ERROR["msg"] = "Adresse d'envoi non configurée"
        print(f"[email non configuré] {to_list} — {subject}")
        return False
    fn = _brevo if cfg.get("mode") == "brevo" else _smtp
    ok = fn(cfg, to_list, subject, text, html, sender_name, attachments)
    if not ok:
        print(f"email error {to_list}: {LAST_ERROR['msg']}")
    return ok


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
        return f"https://wa.me/?text={urllib.parse.quote(text)}" if phone == "" else ""
    return f"https://wa.me/{d}?text={urllib.parse.quote(text)}"
