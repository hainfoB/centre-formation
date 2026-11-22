"""Entreprises clientes, groupes industriels, espace RH, rapports d'émargement par email
et réglage de l'adresse d'envoi (école ou plateforme).

Branché par app.py : entreprise.register(app, globals())."""
import base64
import hashlib
import io
import re
from datetime import date, datetime, timedelta
from functools import wraps

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

import notify
from models import (REPORT_FREQS, Attendance, Company, EmailLog, Enrollment, Formation, PlatformSetting, Seance,
                    Setting, Student, User, db)
from tenancy import unscoped

MAIL_KEYS = ("mode", "host", "port", "security", "user", "password", "api_key", "from_email", "from_name", "reply_to")
SECRET_KEYS = ("password", "api_key")
CODES = {"present": "P", "late": "R", "absent": "A", "excused": "E"}
RH_ENDPOINTS = {"rh_home", "rh_report", "rh_formation", "rh_certificate", "rh_certificates", "logout", "login", "account", "static"}


def register(app, A):   # noqa: C901  (A = app.py globals: helpers shared with the main module)
    _ = A["_"]
    today, parse_date, log, role_required, fmt_da = A["today"], A["parse_date"], A["log"], A["role_required"], A["fmt_da"]
    _EMAIL_RE = A["_EMAIL_RE"]

    # ── Secrets at rest (SMTP password, API key) ──────────────────────────────────────────────────
    def _fernet():
        from cryptography.fernet import Fernet
        key = hashlib.sha256((app.config["SECRET_KEY"] + "|mail").encode()).digest()
        return Fernet(base64.urlsafe_b64encode(key))

    def enc(v):
        return "enc:" + _fernet().encrypt(v.encode()).decode() if v else ""

    def dec(v):
        if not v:
            return ""
        if not v.startswith("enc:"):
            return v
        try:
            return _fernet().decrypt(v[4:].encode()).decode()
        except Exception:  # noqa: BLE001
            return ""

    # ── Sender configuration: the school's own address, else the platform's, else env vars ───────
    def school_mail_raw():
        return {k: Setting.get("mail_" + k) for k in MAIL_KEYS}

    def platform_mail_raw():
        with unscoped():
            return {k: PlatformSetting.get("mail_" + k) for k in MAIL_KEYS}

    def to_cfg(raw):
        if raw.get("mode") not in ("smtp", "brevo") or not raw.get("from_email"):
            return None
        cfg = dict(raw)
        for k in SECRET_KEYS:
            cfg[k] = dec(raw.get(k))
        return cfg

    def resolve_cfg():
        if g.get("school_id"):
            own = to_cfg(school_mail_raw())
            if own:
                return own
        cfg = to_cfg(platform_mail_raw()) or notify._env_cfg()
        if cfg and g.get("school_id"):       # platform address, but signed with the school's name
            cfg = dict(cfg)
            cfg["from_name"] = Setting.get("centre_name") or cfg.get("from_name")
            cfg["reply_to"] = Setting.get("centre_email") or cfg.get("reply_to")
        return cfg

    notify.RESOLVER = resolve_cfg

    def mail_status():
        """(configured, label) for the badges of the settings pages."""
        if g.get("school_id") and to_cfg(school_mail_raw()):
            return True, _("Adresse de l'école") + " : " + Setting.get("mail_from_email")
        p = to_cfg(platform_mail_raw())
        if p:
            return True, _("Adresse de la plateforme") + " : " + p["from_email"]
        e = notify._env_cfg()
        if e and e.get("from_email"):
            return True, _("Adresse de la plateforme") + " : " + e["from_email"]
        return False, _("Aucune adresse d'envoi configurée")

    A["mail_status"] = mail_status

    def send_logged(to, subject, text, html=None, attachments=None, kind="", company_id=None, cfg=None):
        ok = notify.send_email(to, subject, text, html, Setting.get("centre_name") if g.get("school_id") else "AcadémieProLearning",
                               attachments=attachments, cfg=cfg)
        if g.get("school_id"):
            db.session.add(EmailLog(to=", ".join(to if isinstance(to, list) else [to])[:300], subject=subject[:200],
                                    kind=kind, company_id=company_id, ok=ok, error=(notify.LAST_ERROR["msg"] or "")[:300]))
        return ok, notify.LAST_ERROR["msg"]

    def save_mail_form(f, setter, current):
        mode = f.get("mode") if f.get("mode") in ("platform", "smtp", "brevo") else "platform"
        setter("mail_mode", mode)
        for k in ("host", "port", "security", "user", "from_email", "from_name", "reply_to"):
            setter("mail_" + k, (f.get(k) or "").strip()[:200])
        for k in SECRET_KEYS:       # empty field = keep the stored secret
            v = (f.get(k) or "").strip()
            if v:
                setter("mail_" + k, enc(v))
            elif f.get("clear_" + k):
                setter("mail_" + k, "")
        if mode in ("smtp", "brevo") and not _EMAIL_RE.match((f.get("from_email") or "").strip().lower()):
            return _("Adresse d'expédition invalide.")
        return None

    PRESETS = {   # host, port, security — filled in by the "Fournisseur" select
        "gmail": ("smtp.gmail.com", "587", "starttls"), "outlook": ("smtp-mail.outlook.com", "587", "starttls"),
        "office365": ("smtp.office365.com", "587", "starttls"), "ovh": ("ssl0.ovh.net", "465", "ssl"),
        "hostinger": ("smtp.hostinger.com", "465", "ssl"), "zoho": ("smtp.zoho.com", "465", "ssl"),
        "brevo_smtp": ("smtp-relay.brevo.com", "587", "starttls"), "djaweb": ("mail.djaweb.dz", "587", "starttls"),
    }

    def mail_page(scope):
        is_platform = scope == "platform"
        if is_platform:
            def setter(k, v):
                with unscoped():
                    PlatformSetting.set(k, v)
            raw = platform_mail_raw()
        else:
            setter, raw = Setting.set, school_mail_raw()
        if request.method == "POST":
            f = request.form
            if f.get("action") == "test":
                to = (f.get("test_to") or "").strip().lower()
                if not _EMAIL_RE.match(to):
                    flash(_("Adresse de test invalide."), "error")
                else:
                    cfg = to_cfg(raw) if (is_platform or raw.get("mode") in ("smtp", "brevo")) else resolve_cfg()
                    c = A["centre_settings"]()
                    html = notify.html_mail(c["centre_name"], _("Test d'envoi réussi"), [
                        _("Cet email confirme que l'adresse d'envoi fonctionne."),
                        _("Expéditeur : {e}", e=(cfg or {}).get("from_email", "—")),
                        _("Envoyé le {d}", d=A["local_now"]().strftime("%d/%m/%Y %H:%M"))])
                    ok, err = send_logged(to, _("Test d'envoi — {n}", n=c["centre_name"]), _("Test d'envoi réussi."), html,
                                          kind="test", cfg=cfg)
                    if is_platform:
                        with unscoped():
                            PlatformSetting.set("mail_last_test", f"{'OK' if ok else 'ERREUR'} · {A['local_now']():%d/%m/%Y %H:%M} · {err}")
                    db.session.commit()
                    flash(_("Email de test envoyé à {to}. Vérifiez la boîte de réception (et les indésirables).", to=to) if ok
                          else _("Échec de l'envoi : {err}", err=err or _("raison inconnue")), "success" if ok else "error")
            else:
                err = save_mail_form(f, setter, raw)
                if err:
                    db.session.rollback()
                    flash(err, "error")
                else:
                    log("mail_settings", scope)
                    db.session.commit()
                    flash(_("Adresse d'envoi enregistrée. Envoyez un email de test pour la vérifier."), "success")
            return redirect(request.path)
        logs = EmailLog.query.order_by(EmailLog.id.desc()).limit(15).all() if not is_platform else []
        with unscoped():
            last_test = PlatformSetting.get("mail_last_test") if is_platform else ""
        return render_template("email_settings.html", raw=raw, scope=scope, status=mail_status(), presets=PRESETS,
                               has={k: bool(raw.get(k)) for k in SECRET_KEYS}, logs=logs, last_test=last_test,
                               on_railway=bool(__import__('os').environ.get('RAILWAY_ENVIRONMENT')), platform_from=(to_cfg(platform_mail_raw()) or notify._env_cfg() or {}).get("from_email"))

    @app.route("/parametres/email", methods=["GET", "POST"])
    @role_required("admin")
    def mail_settings():
        return mail_page("school")

    @app.route("/plateforme/email", methods=["GET", "POST"])
    @A["super_required"]
    def platform_mail():
        return mail_page("platform")

    # ── RH accounts can only reach their portal ───────────────────────────────────────────────────
    @app.before_request
    def rh_guard():
        if current_user.is_authenticated and getattr(current_user, "role", "") == "rh" \
                and request.endpoint and request.endpoint not in RH_ENDPOINTS and not request.path.startswith("/static"):
            return redirect(url_for("rh_home"))
        return None

    # ── Report data ───────────────────────────────────────────────────────────────────────────────
    def period_from_args(args, default="month"):
        d = today()
        p = args.get("p") or default
        if p == "week":
            d1 = d - timedelta(days=d.weekday())
            d2 = d
        elif p == "lastweek":
            d2 = d - timedelta(days=d.weekday() + 1)
            d1 = d2 - timedelta(days=6)
        elif p == "lastmonth":
            d2 = d.replace(day=1) - timedelta(days=1)
            d1 = d2.replace(day=1)
        elif p == "all":
            d1, d2 = None, d
        elif p == "custom":
            d1, d2 = parse_date(args.get("d1")), parse_date(args.get("d2")) or d
        else:
            p, d1, d2 = "month", d.replace(day=1), d
        return p, d1, d2

    def period_text(d1, d2):
        if not d1:
            return _("Depuis le début des formations, jusqu'au {d}", d=d2.strftime("%d/%m/%Y"))
        return _("Du {a} au {b}", a=d1.strftime("%d/%m/%Y"), b=d2.strftime("%d/%m/%Y"))

    def company_report(scope_ids, d1, d2, formation_id=None):
        """Attendance of the employees of these companies, per formation, over [d1, d2] (séances held)."""
        studs = {s.id: s for s in Student.query.filter(Student.company_id.in_(scope_ids or [0])).all()}
        cos = {c.id: c for c in Company.query.filter(Company.id.in_(scope_ids or [0])).all()}
        q = Enrollment.query.filter(Enrollment.student_id.in_(list(studs) or [0]), Enrollment.status == "confirmed")
        if formation_id:
            q = q.filter(Enrollment.formation_id == formation_id)
        by_fo = {}
        for e in q:
            by_fo.setdefault(e.formation_id, []).append(e)
        blocks, tot = [], {"employees": len(studs), "trained": set(), "present": 0, "late": 0, "absent": 0, "excused": 0,
                           "rates": [], "hours": 0.0}
        horizon = min(d2, today())
        for fid, enrs in sorted(by_fo.items(), key=lambda kv: kv[0]):
            fo = db.session.get(Formation, fid)
            sq = fo.seances.filter(Seance.day <= horizon)
            if d1:
                sq = sq.filter(Seance.day >= d1)
            seances = sq.order_by(Seance.day, Seance.id).all()
            att = {}
            ids = [s.id for s in seances] or [0]
            for a in Attendance.query.filter(Attendance.seance_id.in_(ids), Attendance.enrollment_id.in_([e.id for e in enrs])):
                att[(a.enrollment_id, a.seance_id)] = a
            rows = []
            for e in sorted(enrs, key=lambda e: (studs[e.student_id].last_name.lower(), studs[e.student_id].first_name.lower())):
                st = studs[e.student_id]
                marks, n = [], {"present": 0, "late": 0, "absent": 0, "excused": 0}
                minutes = 0
                for s in seances:
                    a = att.get((e.id, s.id))
                    stt = a.status if a and a.status else "absent"
                    n[stt] = n.get(stt, 0) + 1
                    marks.append(CODES.get(stt, ""))
                    if stt in ("present", "late"):
                        minutes += s.duration_minutes()
                denom = len(seances) - n["excused"]
                rate = round((n["present"] + n["late"]) * 100 / denom) if denom > 0 else 0
                co = cos.get(st.company_id)
                rows.append({"student": st, "enr": e, "name": st.full_name,
                             "sub": " · ".join(x for x in (st.job_title or "", co.name if co and len(scope_ids) > 1 else "") if x),
                             "company": co.name if co else "", "marks": marks, "rate": rate, "hours": round(minutes / 60, 1), **n})
                tot["trained"].add(st.id)
                for k in ("present", "late", "absent", "excused"):
                    tot[k] += n[k]
                tot["hours"] += minutes / 60
                if seances:
                    tot["rates"].append(rate)
            avg = round(sum(r["rate"] for r in rows) / len(rows)) if rows and seances else 0
            trainer = fo.trainer_user.name if getattr(fo, "trainer_user", None) else (fo.trainer or "")
            meta = " · ".join(x for x in (
                _("{n} séance(s) sur la période", n=len(seances)),
                _("{n} salarié(s)", n=len(rows)),
                _("assiduité moyenne {r} %", r=avg) if seances else "",
                _("formateur : {t}", t=trainer) if trainer else "",
                (fo.starts_on.strftime("%d/%m/%Y") + " → " + fo.ends_on.strftime("%d/%m/%Y")) if fo.starts_on and fo.ends_on else "",
            ) if x)
            blocks.append({"formation": fo, "title": fo.title, "meta": meta, "seances": [s.day.strftime("%d/%m") for s in seances],
                           "seance_objs": seances, "rows": rows, "avg": avg, "min": fo.min_attendance or 0})
        tot["avg"] = round(sum(tot["rates"]) / len(tot["rates"])) if tot["rates"] else 0
        tot["trained"] = len(tot["trained"])
        tot["hours"] = round(tot["hours"], 1)
        return blocks, tot

    def report_pdf(title, d1, d2, blocks):
        from pdfs import attendance_report_pdf
        return attendance_report_pdf(Setting.all(), title, period_text(d1, d2), blocks,
                                     A["local_now"]().strftime("%d/%m/%Y %H:%M"))

    def report_xlsx(title, d1, d2, blocks):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws.title = "Synthèse"
        head_fill, head_font = PatternFill("solid", fgColor="0B2545"), Font(bold=True, color="FFFFFF")
        fills = {"P": "DCFCE7", "R": "FEF3C7", "A": "FEE2E2", "E": "E0E7FF"}
        ws.append([Setting.get("centre_name")])
        ws.append([title])
        ws.append([period_text(d1, d2)])
        ws["A1"].font = Font(bold=True, size=14, color="0B2545")
        ws.append([])
        ws.append(["Salarié", "Matricule", "Fonction", "Structure", "Formation", "Séances", "Présent", "Retard", "Absent",
                   "Excusé", "Taux %", "Heures"])
        for c in ws[5]:
            c.fill, c.font = head_fill, head_font
        for b in blocks:
            for r in b["rows"]:
                st = r["student"]
                ws.append([r["name"], st.employee_no or "", st.job_title or "", r["company"], b["title"], len(b["seances"]),
                           r["present"], r["late"], r["absent"], r["excused"], r["rate"], r["hours"]])
        for col, w in zip("ABCDEFGHIJKL", (28, 12, 20, 24, 34, 9, 9, 9, 9, 9, 9, 9)):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "A6"
        for i, b in enumerate(blocks, 1):
            name = re.sub(r"[\\/*?:\[\]]", " ", b["title"])[:26] + f" {i}"
            sh = wb.create_sheet(name)
            sh.append(["Salarié", "Structure"] + b["seances"] + ["Taux %", "Heures"])
            for c in sh[1]:
                c.fill, c.font = head_fill, head_font
                c.alignment = Alignment(horizontal="center")
            for r in b["rows"]:
                sh.append([r["name"], r["company"]] + r["marks"] + [r["rate"], r["hours"]])
                row = sh.max_row
                for j, code in enumerate(r["marks"]):
                    cell = sh.cell(row=row, column=3 + j)
                    cell.alignment = Alignment(horizontal="center")
                    if code in fills:
                        cell.fill = PatternFill("solid", fgColor=fills[code])
            sh.column_dimensions["A"].width, sh.column_dimensions["B"].width = 28, 22
            sh.freeze_panes = "C2"
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def safe_name(s):
        import unicodedata
        s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
        return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:50] or "rapport"

    def report_files(co, d1, d2, blocks, fmt=None):
        fmt = fmt or co.report_format or "both"
        title = _("Émargement — {c}", c=co.name)
        base = f"emargement_{safe_name(co.name)}_{(d1 or d2):%Y%m%d}"
        files = []
        if fmt in ("pdf", "both"):
            files.append((base + ".pdf", report_pdf(title, d1, d2, blocks), "application/pdf"))
        if fmt in ("xlsx", "both"):
            files.append((base + ".xlsx", report_xlsx(title, d1, d2, blocks),
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        return files

    def send_company_report(co, d1, d2, formation_id=None, reason=""):
        to = co.emails()
        if not to:
            return False, _("Aucun email RH renseigné pour cette entreprise.")
        blocks, tot = company_report(co.scope_ids(), d1, d2, formation_id)
        c = A["centre_settings"]()
        paras = [_("Bonjour,"),
                 _("Veuillez trouver ci-joint l'état d'émargement des salariés de <b>{c}</b> — {p}.", c=co.name, p=period_text(d1, d2).lower())]
        if reason:
            paras.append(reason)
        table = "".join(
            f'<tr><td style="padding:6px 8px;border-bottom:1px solid #e5e9f0">{b["title"]}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #e5e9f0;text-align:center">{len(b["rows"])}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #e5e9f0;text-align:center">{len(b["seances"])}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #e5e9f0;text-align:center;font-weight:bold;color:{"#dc2626" if b["avg"] < b["min"] else "#16a34a"}">{b["avg"]} %</td></tr>'
            for b in blocks)
        if table:
            paras.append('<table style="border-collapse:collapse;width:100%;font-size:14px"><tr style="background:#0b2545;color:#fff">'
                         '<th style="padding:6px 8px;text-align:left">Formation</th><th>Salariés</th><th>Séances</th><th>Assiduité</th></tr>'
                         + table + "</table>")
        low = [(r["name"], b["title"], r["rate"]) for b in blocks for r in b["rows"] if b["seances"] and r["rate"] < b["min"]]
        if low:
            paras.append(_("<b>Assiduité insuffisante</b> (sous le seuil de l'attestation) : ") +
                         ", ".join(f"{n} ({r} %, {t})" for n, t, r in low[:15]) + ("…" if len(low) > 15 else ""))
        paras.append(_("Présences : {p} · retards : {r} · absences : {a} · heures de formation suivies : {h} h.",
                       p=tot["present"], r=tot["late"], a=tot["absent"], h=tot["hours"]))
        html = notify.html_mail(c["centre_name"], _("Rapport d'émargement"), paras,
                                (_("Consulter sur la plateforme"), A["base_url"]() + url_for("login")))
        text = re.sub("<[^>]+>", "", "\n".join(paras))
        ok, err = send_logged(to, _("Émargement {c} — {p}", c=co.name, p=period_text(d1, d2)), text, html,
                              attachments=report_files(co, d1, d2, blocks), kind="rh_report", company_id=co.id)
        if ok:
            co.last_report_at = datetime.utcnow()
        return ok, err

    A["company_report"] = company_report
    A["send_company_report"] = send_company_report

    # ── Scheduled reports (hourly job, sends once the local time is past 07:00) ──────────────────
    def _run_company_reports():
        now = A["local_now"]()
        if now.hour < 7:
            return 0
        d, sent = now.date(), 0
        for co in Company.query.filter(Company.report_freq.in_(("weekly", "monthly", "end"))).all():
            if not co.emails():
                continue
            key = d1 = d2 = fid = None
            reason = ""
            if co.report_freq == "weekly" and d.weekday() == 0:
                d2 = d - timedelta(days=1)
                d1, key = d2 - timedelta(days=6), f"W{d:%G%V}"
                reason = _("Rapport hebdomadaire automatique.")
            elif co.report_freq == "monthly" and d.day == 1:
                d2 = d - timedelta(days=1)
                d1, key = d2.replace(day=1), f"M{d2:%Y%m}"
                reason = _("Rapport mensuel automatique.")
            elif co.report_freq == "end":
                ended = (Formation.query.join(Enrollment).join(Student)
                         .filter(Student.company_id.in_(co.scope_ids()), Formation.ends_on == d - timedelta(days=1)).first())
                if ended:
                    fid, d1, d2, key = ended.id, None, ended.ends_on, f"E{d:%Y%m%d}{ended.id}"
                    reason = _("La formation « {t} » s'est terminée : voici l'état complet des présences.", t=ended.title)
            if key and co.last_report_key != key:
                ok, _err = send_company_report(co, d1, d2, fid, reason)
                if ok:
                    co.last_report_key = key
                    sent += 1
                db.session.commit()
        return sent

    run_company_reports = A["_each_school"](_run_company_reports)
    A["run_company_reports"] = run_company_reports

    # ── Companies (admin / secretariat) ───────────────────────────────────────────────────────────
    def apply_company_form(co, f):
        co.name = (f.get("name") or co.name or "").strip()[:160]
        co.kind = "group" if f.get("kind") == "group" else "company"
        pid = f.get("parent_id", type=int)
        co.parent_id = pid if (co.kind == "company" and pid and pid != co.id) else None
        for k, n in (("sector", 120), ("address", 200), ("nif", 40), ("contact_name", 120), ("contact_phone", 30)):
            if k in f:
                setattr(co, k, (f.get(k) or "").strip()[:n] or None)
        if "hr_emails" in f:
            co.hr_emails = ", ".join(e for e in re.split(r"[,;\s]+", f.get("hr_emails") or "") if _EMAIL_RE.match(e.lower()))[:1000] or None
        if f.get("report_freq") in REPORT_FREQS:
            co.report_freq = f.get("report_freq")
        if f.get("report_format") in ("pdf", "xlsx", "both"):
            co.report_format = f.get("report_format")

    def companies_tree():
        allc = Company.query.order_by(Company.name).all()
        groups = [c for c in allc if c.kind == "group"]
        counts = dict(db.session.query(Student.company_id, db.func.count(Student.id))
                      .filter(Student.company_id.isnot(None)).group_by(Student.company_id).all())
        return allc, groups, counts

    @app.route("/entreprises", methods=["GET", "POST"])
    @role_required("manage")
    def companies():
        if request.method == "POST":
            if not (request.form.get("name") or "").strip():
                flash(_("Le nom est obligatoire."), "error")
                return redirect(url_for("companies"))
            co = Company()
            apply_company_form(co, request.form)
            db.session.add(co)
            log("company_created", co.name)
            db.session.commit()
            flash(_("« {n} » créée. Ajoutez maintenant ses salariés et l'email du responsable RH.", n=co.name), "success")
            return redirect(url_for("company_detail", cid=co.id))
        allc, groups, counts = companies_tree()
        return render_template("companies.html", allc=allc, groups=groups, counts=counts,
                               rh=dict(db.session.query(User.company_id, db.func.count(User.id)).filter(User.role == "rh")
                                       .group_by(User.company_id).all()))

    @app.route("/entreprises/<int:cid>", methods=["GET", "POST"])
    @role_required("manage")
    def company_detail(cid):
        co = Company.query.get_or_404(cid)
        if request.method == "POST":
            f, act = request.form, request.form.get("action")
            if act == "save":
                apply_company_form(co, f)
                flash(_("Entreprise enregistrée."), "success")
            elif act == "add_students":
                ids = [int(x) for x in f.getlist("student_ids") if x.isdigit()]
                for st in Student.query.filter(Student.id.in_(ids or [0])):
                    st.company_id = co.id
                flash(_("{n} salarié(s) rattaché(s).", n=len(ids)), "success")
            elif act == "remove_student":
                st = Student.query.get_or_404(f.get("student_id", type=int))
                if st.company_id == co.id:
                    st.company_id = None
            elif act == "enroll":     # enrol all the selected employees in a formation at once
                fo = Formation.query.get_or_404(f.get("formation_id", type=int))
                scope = set(co.scope_ids()) | ({co.parent_id} if co.parent_id else set())
                if not (fo.client_company_id in scope or (fo.for_companies and not fo.client_company_id)):
                    abort(400)
                ids = [int(x) for x in f.getlist("student_ids") if x.isdigit()]
                n = 0
                for st in Student.query.filter(Student.id.in_(ids or [0]), Student.company_id.in_(co.scope_ids())):
                    if not Enrollment.query.filter_by(formation_id=fo.id, student_id=st.id).first():
                        e = Enrollment(formation_id=fo.id, student_id=st.id, status="pending", source="entreprise")
                        db.session.add(e)
                        db.session.flush()
                        A["confirm_enrollment"](e)
                        n += 1
                flash(_("{n} salarié(s) inscrit(s) à « {t} ».", n=n, t=fo.title), "success")
            elif act == "create_rh":
                email = (f.get("email") or "").strip().lower()
                with unscoped():
                    taken = User.query.filter_by(email=email).first()
                if not _EMAIL_RE.match(email) or taken or not (f.get("name") or "").strip():
                    flash(_("Nom et email (unique) requis."), "error")
                else:
                    import secrets
                    pw = secrets.token_urlsafe(8)
                    u = User(name=f["name"].strip()[:120], email=email, role="rh", company_id=co.id, phone=(f.get("phone") or "")[:30] or None)
                    u.set_password(pw)
                    db.session.add(u)
                    if email not in co.emails() and f.get("add_to_reports"):
                        co.hr_emails = ", ".join(co.emails() + [email])
                    c = A["centre_settings"]()
                    sent = False
                    if f.get("send_access"):
                        html = notify.html_mail(c["centre_name"], _("Votre accès à l'espace RH"), [
                            _("Bonjour {n},", n=u.name),
                            _("{s} vous ouvre un espace pour suivre l'émargement et l'assiduité des salariés de <b>{c}</b>.", s=c["centre_name"], c=co.name),
                            _("Identifiant : <b>{e}</b><br>Mot de passe provisoire : <b>{p}</b>", e=email, p=pw),
                            _("Pensez à changer le mot de passe dans « Mon compte » après la première connexion.")],
                            (_("Se connecter"), A["base_url"]() + url_for("login")))
                        sent, _e = send_logged(email, _("Accès espace RH — {c}", c=c["centre_name"]),
                                               f"Identifiant : {email}\nMot de passe : {pw}\n{A['base_url']()}{url_for('login')}",
                                               html, kind="rh_access", company_id=co.id)
                    flash(_("Compte RH créé. Identifiant : {e} · mot de passe : {p}", e=email, p=pw) +
                          (" · " + _("accès envoyé par email") if sent else ""), "success")
            elif act == "rh_toggle":
                u = User.query.get_or_404(f.get("user_id", type=int))
                if u.role == "rh" and u.company_id == co.id:
                    u.active = not u.active
            elif act == "rh_reset":
                import secrets
                u = User.query.get_or_404(f.get("user_id", type=int))
                if u.role == "rh" and u.company_id == co.id:
                    pw = secrets.token_urlsafe(8)
                    u.set_password(pw)
                    flash(_("Nouveau mot de passe de {name} : {pw}", name=u.name, pw=pw), "success")
            elif act == "send_report":
                _p, d1, d2 = period_from_args(f)
                ok, err = send_company_report(co, d1, d2, f.get("formation_id", type=int))
                flash(_("Rapport envoyé à {to}.", to=", ".join(co.emails())) if ok else _("Échec de l'envoi : {err}", err=err),
                      "success" if ok else "error")
            elif act == "delete":
                if Student.query.filter_by(company_id=co.id).count() or Company.query.filter_by(parent_id=co.id).count():
                    flash(_("Retirez d'abord les salariés et les structures rattachées."), "error")
                else:
                    User.query.filter_by(company_id=co.id, role="rh").update({"active": False})
                    db.session.delete(co)
                    log("company_deleted", co.name)
                    db.session.commit()
                    return redirect(url_for("companies"))
            log("company_changed", f"{co.name} · {act}")
            db.session.commit()
            return redirect(url_for("company_detail", cid=co.id, tab=f.get("tab") or None))
        p, d1, d2 = period_from_args(request.args)
        fid = request.args.get("formation_id", type=int)
        blocks, tot = company_report(co.scope_ids(), d1, d2, fid)
        employees = Student.query.filter(Student.company_id.in_(co.scope_ids())).order_by(Student.last_name, Student.first_name).all()
        free = Student.query.filter(Student.company_id.is_(None)).order_by(Student.last_name, Student.first_name).limit(500).all()
        allc, groups, counts = companies_tree()
        scope = set(co.scope_ids()) | ({co.parent_id} if co.parent_id else set())
        fos = [f for f in Formation.query.filter(Formation.status != "done").order_by(Formation.starts_on.desc().nullslast()).all()
               if f.client_company_id in scope or (f.for_companies and not f.client_company_id)]   # never school support classes
        return render_template("company_detail.html", co=co, blocks=blocks, tot=tot, employees=employees, free=free, groups=groups,
                               counts=counts, children=co.children(), rh_users=User.query.filter_by(role="rh", company_id=co.id).all(),
                               logs=EmailLog.query.filter_by(company_id=co.id).order_by(EmailLog.id.desc()).limit(10).all(),
                               p=p, d1=d1, d2=d2, fid=fid, fos=fos, period=period_text(d1, d2), tab=request.args.get("tab", "suivi"),
                               freqs=REPORT_FREQS, all_formations=Formation.query.order_by(Formation.title).all(),
                               certs=cert_rows(cert_scope_from(co, request.args), fid) if request.args.get("tab") == "attestations" else [],
                               structure=request.args.get("structure", type=int),
                               scope_cos=Company.query.filter(Company.id.in_(co.scope_ids())).order_by(Company.name).all())

    def report_download(scope_ids, title_name, fmt, args):
        _p, d1, d2 = period_from_args(args)
        blocks, _t = company_report(scope_ids, d1, d2, args.get("formation_id", type=int))
        title = _("Émargement — {c}", c=title_name)
        base = f"emargement_{safe_name(title_name)}_{(d1 or d2):%Y%m%d}"
        if fmt == "pdf":
            return send_file(io.BytesIO(report_pdf(title, d1, d2, blocks)), mimetype="application/pdf", download_name=base + ".pdf")
        return send_file(io.BytesIO(report_xlsx(title, d1, d2, blocks)), download_name=base + ".xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/entreprises/<int:cid>/emargement.<fmt>")
    @role_required("manage")
    def company_report_file(cid, fmt):
        co = Company.query.get_or_404(cid)
        if fmt not in ("pdf", "xlsx"):
            abort(404)
        return report_download(co.scope_ids(), co.name, fmt, request.args)

    # ── RH portal ─────────────────────────────────────────────────────────────────────────────────
    def rh_required(fn):
        @wraps(fn)
        @login_required
        def inner(*a, **kw):
            if current_user.role != "rh" and not current_user.can("manage"):
                abort(403)
            return fn(*a, **kw)
        return inner

    def rh_scope():
        """(root company, allowed ids, selected ids). Staff may preview any company with ?cid=."""
        if current_user.role == "rh":
            root = db.session.get(Company, current_user.company_id) if current_user.company_id else None
            if not root:
                abort(403)
        else:
            root = Company.query.get_or_404(request.args.get("cid", type=int) or 0)
        allowed = root.scope_ids()
        sel = request.args.get("structure", type=int)
        return root, allowed, ([sel] if sel in allowed else allowed)

    @app.route("/rh")
    @rh_required
    def rh_home():
        root, allowed, sel = rh_scope()
        p, d1, d2 = period_from_args(request.args)
        fid = request.args.get("formation_id", type=int)
        blocks, tot = company_report(sel, d1, d2, fid)
        all_blocks, _t = company_report(allowed, None, today())
        fos = [b["formation"] for b in all_blocks]
        certs = [{"name": x["student"].full_name, "company": x["structure"], "formation": x["formation"], "enr": x["enr"],
                  "ok": x["ok"], "why": x["why"]} for x in cert_rows(allowed)]
        return render_template("rh_home.html", root=root, structures=Company.query.filter(Company.id.in_(allowed)).order_by(Company.name).all(),
                               sel=sel if len(sel) == 1 else None, blocks=blocks, tot=tot, p=p, d1=d1, d2=d2, fid=fid, fos=fos,
                               period=period_text(d1, d2), certs=certs, preview=current_user.role != "rh",
                               tab=request.args.get("tab", "suivi"))

    @app.route("/rh/emargement.<fmt>")
    @rh_required
    def rh_report(fmt):
        root, allowed, sel = rh_scope()
        if fmt not in ("pdf", "xlsx"):
            abort(404)
        name = root.name if len(sel) > 1 else db.session.get(Company, sel[0]).name
        return report_download(sel, name, fmt, request.args)

    @app.route("/rh/formation/<int:fid>")
    @rh_required
    def rh_formation(fid):
        root, allowed, sel = rh_scope()
        p, d1, d2 = period_from_args(request.args, default="all")
        blocks, tot = company_report(sel, d1, d2, fid)
        if not blocks:
            abort(404)
        return render_template("rh_formation.html", root=root, b=blocks[0], p=p, d1=d1, d2=d2, period=period_text(d1, d2),
                               preview=current_user.role != "rh")

    @app.route("/rh/attestation/<int:eid>")
    @rh_required
    def rh_certificate(eid):
        root, allowed, sel = rh_scope()
        enr = Enrollment.query.get_or_404(eid)
        if enr.student.company_id not in allowed:
            abort(403)
        row = next((x for x in cert_rows(allowed, enr.formation_id) if x["enr"].id == enr.id), None)
        ok, why = (row["ok"], row["why"]) if row else (False, "")
        if not ok:
            flash(_("Attestation non disponible : {why}.", why=why), "error")
            return redirect(url_for("rh_home", tab="attestations", cid=request.args.get("cid")))
        return A["certificate_response"](enr)

    # ── Attestations par entreprise / groupe / structure ──────────────────────────────────────────
    def ensure_cert_no(e):
        if not e.cert_no:
            e.cert_issued_on = e.cert_issued_on or today()
            db.session.flush()
            e.cert_no = f"ATT-{e.cert_issued_on.year}-{e.id:05d}"

    def cert_rows(scope_ids, formation_id=None, only_ok=False):
        """One row per (employee, formation) in the scope, with eligibility."""
        studs = {s.id: s for s in Student.query.filter(Student.company_id.in_(scope_ids or [0]))}
        cos = {c.id: c for c in Company.query.filter(Company.id.in_(scope_ids or [0]))}
        q = Enrollment.query.filter(Enrollment.student_id.in_(list(studs) or [0]), Enrollment.status == "confirmed")
        if formation_id:
            q = q.filter(Enrollment.formation_id == formation_id)
        by_fo, rows = {}, []
        for e in q:
            by_fo.setdefault(e.formation_id, []).append(e)
        for fid, enrs in by_fo.items():
            fo = db.session.get(Formation, fid)
            stats = {r["enr"].id: r for r in A["attendance_stats"](fo)["rows"]}
            for e in enrs:
                st, r = studs[e.student_id], stats.get(e.id)
                ok, why = A["cert_eligible"](e, r)
                if ok and fo.status != "done" and (not fo.ends_on or fo.ends_on >= today()):
                    ok, why = False, _("Formation en cours (fin le {d})", d=fo.ends_on.strftime("%d/%m/%Y") if fo.ends_on else "—")
                if only_ok and not ok:
                    continue
                co = cos.get(st.company_id)
                rows.append({"enr": e, "student": st, "formation": fo, "stats": r or {}, "ok": ok, "why": why,
                             "company": co, "structure": co.name if co else ""})
        rows.sort(key=lambda x: (x["structure"].lower(), x["formation"].title.lower(), x["student"].last_name.lower(),
                                 x["student"].first_name.lower()))
        return rows

    def slip_rows(rows):
        out = []
        for x in rows:
            fo, st, s = x["formation"], x["student"], x["stats"]
            per = " → ".join(d.strftime("%d/%m/%Y") for d in (fo.starts_on, fo.ends_on) if d)
            out.append({"name": st.full_name, "matricule": st.employee_no or "", "job": st.job_title or "", "structure": x["structure"],
                        "formation": fo.title, "period": per, "hours": s.get("hours") or fo.hours_total or "",
                        "rate": s.get("rate", 0), "cert_no": x["enr"].cert_no or ""})
        return out

    def cert_bundle(label, rows, recipients):
        """(combined_pdf, zip_bytes, slip_pdf) for these eligible rows; numbers the certificates."""
        import zipfile
        from pdfs import certificate_pdf, certificates_pdf, transmittal_pdf
        centre = Setting.all()
        for x in rows:
            ensure_cert_no(x["enr"])
        db.session.commit()
        base = A["base_url"]()
        items = [(x["student"], x["formation"], x["enr"], x["stats"], f"{base}/verifier/{x['enr'].token}") for x in rows]
        ref = f"BE-{today():%Y%m%d}-{len(rows):03d}{(rows[0]['company'].id if rows and rows[0]['company'] else 0):03d}"
        slip = transmittal_pdf(centre, label, recipients, slip_rows(rows), A["local_now"]().strftime("%d/%m/%Y %H:%M"), ref)
        combined = certificates_pdf(centre, items) if items else b""
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("00_bordereau_envoi.pdf", slip)
            for x, it in zip(rows, items):
                path = "/".join(p for p in (safe_name(x["structure"]) if x["structure"] else "Particuliers",
                                            safe_name(x["formation"].title)) if p)
                z.writestr(f"{path}/attestation_{safe_name(x['student'].last_name)}_{safe_name(x['student'].first_name)}.pdf",
                           certificate_pdf(centre, *it))
        return combined, zbuf.getvalue(), slip, ref

    def cert_scope_from(co, args):
        allowed = co.scope_ids()
        sel = args.get("structure", type=int)
        return [sel] if sel in allowed else allowed

    def cert_download(label, rows, recipients, kind):
        rows = [x for x in rows if x["ok"]]
        if not rows:
            flash(_("Aucun salarié ne remplit encore les conditions de l'attestation."), "error")
            return redirect(request.referrer or url_for("rh_home" if current_user.role == "rh" else "dashboard", tab="attestations"))
        combined, zbytes, slip, ref = cert_bundle(label, rows, recipients)
        log("certificates_company", f"{label} ({len(rows)}, {kind})")
        db.session.commit()
        name = f"attestations_{safe_name(label)}_{today():%Y%m%d}"
        if kind == "zip":
            return send_file(io.BytesIO(zbytes), mimetype="application/zip", download_name=name + ".zip")
        if kind == "slip":
            return send_file(io.BytesIO(slip), mimetype="application/pdf", download_name=f"bordereau_{safe_name(label)}.pdf")
        return send_file(io.BytesIO(combined), mimetype="application/pdf", download_name=name + ".pdf")

    def send_certificates(co, rows, fmt="pdf"):
        rows = [x for x in rows if x["ok"]]
        to = co.emails()
        if not to:
            return False, _("Aucun email RH renseigné pour cette entreprise.")
        if not rows:
            return False, _("Aucun salarié ne remplit encore les conditions de l'attestation.")
        combined, zbytes, slip, ref = cert_bundle(co.name, rows, to)
        c = A["centre_settings"]()
        name = f"attestations_{safe_name(co.name)}_{today():%Y%m%d}"
        files = [(f"bordereau_{ref}.pdf", slip, "application/pdf")]
        files.append((name + ".zip", zbytes, "application/zip") if fmt == "zip" else (name + ".pdf", combined, "application/pdf"))
        fos = sorted({x["formation"].title for x in rows})
        paras = [_("Bonjour,"),
                 _("Veuillez trouver ci-joint {n} attestation(s) de formation des salariés de <b>{c}</b>, accompagnées du bordereau d'envoi n° {r}.",
                   n=len(rows), c=co.name, r=ref),
                 _("Formation(s) : {f}.", f=", ".join(fos)),
                 _("Chaque attestation porte un QR code qui permet d'en vérifier l'authenticité en ligne.")]
        html = notify.html_mail(c["centre_name"], _("Attestations de formation"), paras,
                                (_("Consulter sur la plateforme"), A["base_url"]() + url_for("login")))
        ok, err = send_logged(to, _("Attestations de formation — {c} ({n})", c=co.name, n=len(rows)),
                              re.sub("<[^>]+>", "", "\n".join(paras)), html, attachments=files, kind="certificates", company_id=co.id)
        return ok, err

    @app.route("/entreprises/<int:cid>/attestations.<kind>")
    @role_required("manage")
    def company_certificates(cid, kind):
        co = Company.query.get_or_404(cid)
        if kind not in ("pdf", "zip", "slip"):
            abort(404)
        scope = cert_scope_from(co, request.args)
        label = co.name if len(scope) > 1 else db.session.get(Company, scope[0]).name
        return cert_download(label, cert_rows(scope, request.args.get("formation_id", type=int)), co.emails(), kind)

    @app.route("/entreprises/<int:cid>/attestations/envoyer", methods=["POST"])
    @role_required("manage")
    def company_certificates_send(cid):
        co = Company.query.get_or_404(cid)
        f = request.form
        rows = cert_rows(cert_scope_from(co, f), f.get("formation_id", type=int))
        ok, err = send_certificates(co, rows, f.get("fmt", "pdf"))
        db.session.commit()
        flash(_("Attestations envoyées à {to}.", to=", ".join(co.emails())) if ok else _("Échec de l'envoi : {err}", err=err),
              "success" if ok else "error")
        return redirect(url_for("company_detail", cid=co.id, tab="attestations", formation_id=f.get("formation_id") or None,
                                structure=f.get("structure") or None))

    @app.route("/rh/attestations.<kind>")
    @rh_required
    def rh_certificates(kind):
        root, allowed, sel = rh_scope()
        if kind not in ("pdf", "zip"):
            abort(404)
        label = root.name if len(sel) > 1 else db.session.get(Company, sel[0]).name
        return cert_download(label, cert_rows(sel, request.args.get("formation_id", type=int)), [], kind)

    @app.route("/formations/<int:fid>/attestations-par-entreprise.zip")
    @role_required("manage")
    def formation_certificates_by_company(fid):
        """One folder per company (+ « Particuliers »), each with its slip and one PDF per trainee."""
        import zipfile
        from pdfs import certificate_pdf, transmittal_pdf
        fo = Formation.query.get_or_404(fid)
        if fo.status != "done" and (not fo.ends_on or fo.ends_on >= today()):
            flash(_("Formation en cours (fin le {d})", d=fo.ends_on.strftime("%d/%m/%Y") if fo.ends_on else "—"), "error")
            return redirect(url_for("formation_detail", fid=fo.id, tab="assiduite"))
        stats = A["attendance_stats"](fo)
        centre, base = Setting.all(), A["base_url"]()
        groups = {}
        for r in stats["rows"]:
            e = r["enr"]
            if not A["cert_eligible"](e, r)[0]:
                continue
            ensure_cert_no(e)
            co = db.session.get(Company, e.student.company_id) if e.student.company_id else None
            groups.setdefault(co.name if co else "", []).append({"enr": e, "student": e.student, "formation": fo, "stats": r,
                                                                 "company": co, "structure": co.name if co else ""})
        if not groups:
            flash(_("Aucun stagiaire ne remplit encore les conditions de l'attestation."), "error")
            return redirect(url_for("formation_detail", fid=fo.id, tab="assiduite"))
        db.session.commit()
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            for label, rows in sorted(groups.items()):
                folder = safe_name(label) if label else "Particuliers"
                co = rows[0]["company"]
                z.writestr(f"{folder}/00_bordereau_envoi.pdf", transmittal_pdf(
                    centre, label or _("Particuliers"), co.emails() if co else [], slip_rows(rows),
                    A["local_now"]().strftime("%d/%m/%Y %H:%M"), f"BE-{today():%Y%m%d}-{fo.id}-{folder[:8].upper()}"))
                for x in rows:
                    z.writestr(f"{folder}/attestation_{safe_name(x['student'].last_name)}_{safe_name(x['student'].first_name)}.pdf",
                               certificate_pdf(centre, x["student"], fo, x["enr"], x["stats"], f"{base}/verifier/{x['enr'].token}"))
        log("certificates_by_company", f"{fo.title} ({sum(len(v) for v in groups.values())})")
        db.session.commit()
        return send_file(io.BytesIO(zbuf.getvalue()), mimetype="application/zip",
                         download_name=f"attestations_par_entreprise_{fo.slug[:40]}.zip")

    A["cert_rows"] = cert_rows

    # ── Template helpers ──────────────────────────────────────────────────────────────────────────
    @app.context_processor
    def _ent_ctx():
        def company_options():
            return Company.query.order_by(Company.name).all() if g.get("school_id") else []
        rh_company = None
        if current_user.is_authenticated and getattr(current_user, "role", "") == "rh" and current_user.company_id:
            rh_company = db.session.get(Company, current_user.company_id)
        return {"company_options": company_options, "rh_company": rh_company, "mail_status": mail_status}
