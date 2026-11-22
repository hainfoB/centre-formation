"""Au-delà de Moodle : badges et attestation e-learning automatiques, classes virtuelles avec présence
pointée dans l'émargement, suivi e-learning dans l'espace RH, et assistant de génération de questions
(IA Claude si une clé est configurée, sinon générateur intégré à partir du contenu des pages).

Branché par app.py : extras.register(app, globals())  (après community)."""
import io
import json
import os
import random
import re
import urllib.request
from datetime import datetime, timedelta

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from models import (Activity, ActivityCompletion, Attendance, Company, Course, CourseAward, Enrollment, Question,
                    Seance, Setting, Student, db)


def register(app, A):  # noqa: C901
    _ = A["_"]
    log, today = A["log"], A["today"]
    app.jinja_env.globals["KIND_ICONS"].update({"live": "camera-video"})

    def get_course(cid):
        c = Course.query.get_or_404(cid)
        fo = c.formation
        if not (current_user.is_authenticated and current_user.role != "rh" and
                (current_user.can("manage") or (current_user.role == "formateur" and fo.trainer_id == current_user.id))):
            abort(403)
        return c

    # ── Badges & e-learning certificate ───────────────────────────────────────────────────────────
    def course_avg(c, enr):
        items = [x for x in c.activities(True) if x.kind in ("quiz", "assign")]
        vals = [A["item_pct"](x, enr.id) for x in items]
        got = [v for v in vals if v is not None]
        return (round(sum(got) / len(got), 1) if got else None), len(items)

    def check_award(c, enr):
        o = c.opts()
        if o.get("badge_off") or c.status != "published":
            return None
        aw = CourseAward.query.filter_by(course_id=c.id, enrollment_id=enr.id).first()
        if aw:
            return aw
        pr = A["el_progress"](c, enr)
        if not pr["total"] or pr["pct"] < 100:
            return None
        avg, n_items = course_avg(c, enr)
        if o.get("min_grade") and n_items and (avg is None or avg < float(o["min_grade"]) * 5):
            return None
        aw = CourseAward(course_id=c.id, enrollment_id=enr.id, grade=avg)
        db.session.add(aw)
        db.session.commit()
        return aw

    A["check_award"] = check_award

    def award_pdf(aw):
        from reportlab.lib.colors import HexColor, white
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfgen import canvas

        import pdfs
        c = db.session.get(Course, aw.course_id)
        e = db.session.get(Enrollment, aw.enrollment_id)
        centre = Setting.all()
        buf = io.BytesIO()
        W, H = landscape(A4)
        cv = canvas.Canvas(buf, pagesize=(W, H))
        NAVY, OR = HexColor("#0b2545"), HexColor("#f97316")
        cv.setFillColor(NAVY)
        cv.rect(0, 0, W, H, fill=1, stroke=0)
        cv.setFillColor(white)
        cv.roundRect(28, 28, W - 56, H - 56, 18, fill=1, stroke=0)
        cv.setFillColor(OR)
        cv.rect(28, H - 40, W - 56, 12, fill=1, stroke=0)
        # badge medal
        bx, by = 140, H / 2 + 10
        cv.setFillColor(OR)
        cv.circle(bx, by, 70, fill=1, stroke=0)
        cv.setFillColor(NAVY)
        cv.circle(bx, by, 56, fill=1, stroke=0)
        import math
        cv.setFillColor(OR)
        pth = cv.beginPath()
        for k in range(10):
            r_ = 26 if k % 2 == 0 else 11
            ang = math.pi / 2 + k * math.pi / 5
            px, py = bx + r_ * math.cos(ang), by + 8 + r_ * math.sin(ang)
            pth.moveTo(px, py) if k == 0 else pth.lineTo(px, py)
        pth.close()
        cv.drawPath(pth, fill=1, stroke=0)
        cv.setFillColor(white)
        cv.setFont("Helvetica-Bold", 9)
        cv.drawCentredString(bx, by - 30, "E-LEARNING")
        cv.setFillColor(NAVY)
        badge = c.opts().get("badge_name") or c.title
        pdfs._fit(cv, badge, bx, by - 98, 210, 11, align="center", bold=True)
        x0 = 260
        cv.setFillColor(OR)
        cv.setFont("Helvetica-Bold", 12)
        cv.drawString(x0, H - 110, (centre.get("centre_name") or "").upper())
        cv.setFillColor(NAVY)
        cv.setFont("Helvetica-Bold", 30)
        cv.drawString(x0, H - 155, "Attestation de réussite")
        cv.setFont("Helvetica", 13)
        cv.setFillColor(HexColor("#42506b"))
        cv.drawString(x0, H - 180, "Parcours de formation en ligne")
        cv.setFillColor(NAVY)
        cv.setFont("Helvetica", 12)
        cv.drawString(x0, H - 225, "Décernée à")
        pdfs._fit(cv, e.student.full_name, x0, H - 258, W - x0 - 60, 26, bold=True)
        cv.setFont("Helvetica", 12)
        cv.drawString(x0, H - 290, "pour avoir suivi avec succès l'intégralité du parcours")
        pdfs._fit(cv, c.title, x0, H - 315, W - x0 - 60, 16, bold=True)
        mins = sum(a.duration_min or 0 for a in c.activities(True))
        info = [f"Formation : {c.formation.title}"]
        if mins:
            info.append(f"Durée estimée : {round(mins / 60, 1)} h")
        if aw.grade is not None:
            info.append(f"Moyenne aux évaluations : {('%g' % round(aw.grade / 5, 2)).replace('.', ',')} / 20")
        info.append(f"Délivrée le {aw.issued_at:%d/%m/%Y} · n° {aw.code.upper()}")
        cv.setFont("Helvetica", 10.5)
        cv.setFillColor(HexColor("#42506b"))
        y = H - 350
        for line in info:
            pdfs._fit(cv, line, x0, y, W - x0 - 180, 10.5)
            y -= 16
        url = f"{A['base_url']()}/badge/{aw.code}"
        try:
            import segno
            q = io.BytesIO()
            segno.make(url, error="m").save(q, kind="png", scale=4, border=1, dark="#0b2545")
            q.seek(0)
            from reportlab.lib.utils import ImageReader
            cv.drawImage(ImageReader(q), W - 150, 60, 96, 96)
            cv.setFont("Helvetica", 7.5)
            cv.drawCentredString(W - 102, 50, "Vérifier l'authenticité")
        except Exception:  # noqa: BLE001
            pass
        cv.setFillColor(NAVY)
        cv.setFont("Helvetica-Bold", 10)
        cv.drawString(x0, 90, centre.get("director_name") or "")
        cv.setFont("Helvetica", 9)
        cv.drawString(x0, 76, "Le directeur de l'établissement")
        cv.save()
        return buf.getvalue()

    @app.route("/t/<token>/badge/<int:cid>.pdf")
    def learn_award_pdf(token, cid):
        enr = Enrollment.query.filter_by(token=token).first_or_404()
        aw = CourseAward.query.filter_by(course_id=cid, enrollment_id=enr.id).first_or_404()
        return send_file(io.BytesIO(award_pdf(aw)), mimetype="application/pdf",
                         download_name=f"attestation_elearning_{aw.code}.pdf")

    @app.route("/badge/<code>")
    def badge_verify(code):
        from tenancy import unscoped
        with unscoped():
            aw = CourseAward.query.filter_by(code=code).first()
            data = None
            if aw:
                c, e = db.session.get(Course, aw.course_id), db.session.get(Enrollment, aw.enrollment_id)
                from models import School
                sc = db.session.get(School, aw.school_id)
                data = {"name": e.student.full_name, "course": c.title, "badge": c.opts().get("badge_name") or c.title,
                        "formation": c.formation.title, "school": sc.name if sc else "", "date": aw.issued_at, "grade": aw.grade,
                        "code": aw.code}
        return render_template("badge_verify.html", d=data)

    @app.route("/elearning/<int:cid>/badge", methods=["POST"])
    @login_required
    def el_badge_settings(cid):
        c = get_course(cid)
        f = request.form
        o = c.opts()
        o.update({"badge_off": not f.get("badge_on"), "badge_name": (f.get("badge_name") or "").strip()[:80],
                  "min_grade": A["to_int"](f.get("min_grade"), 0, 20) or None})
        c.set_opts(o)
        db.session.commit()
        flash(_("Réglages du badge enregistrés."), "success")
        return redirect(url_for("el_course", cid=c.id))

    # ── Virtual classroom (live session counted in the attendance sheet) ──────────────────────────
    def live_times(a):
        o = a.opts()
        try:
            start = datetime.strptime(o.get("start") or "", "%Y-%m-%dT%H:%M")
        except ValueError:
            return None, None
        return start, start + timedelta(minutes=int(o.get("duration") or 90))

    def save_live(a, f):
        o = {"start": (f.get("start") or "").strip(), "duration": A["to_int"](f.get("duration"), 15, 600) or 90,
             "link_attendance": bool(f.get("link_attendance"))}
        a.url = a.url or (f.get("url") or "").strip()[:500] or None
        old = a.opts()
        a.set_opts({**old, **o})
        start, end = live_times(a)
        if not a.url:
            a.url = None
        if start and o["link_attendance"]:
            c = db.session.get(Course, a.course_id)
            label = f"{start:%H:%M} - {end:%H:%M}"
            s = db.session.get(Seance, old.get("seance_id")) if old.get("seance_id") else None
            if not s:
                s = Seance(formation_id=c.formation_id, day=start.date(), time_label=label, topic=f"{_('Classe virtuelle')} : {a.title}"[:200])
                db.session.add(s)
                db.session.flush()
            s.day, s.time_label = start.date(), label
            o2 = a.opts()
            o2["seance_id"] = s.id
            a.set_opts(o2)
        a.completion = "none" if f.get("completion") == "none" else "auto"

    prev_save = A.get("save_activity_opts")

    def save_any(a, f):
        if a.kind == "live":
            save_live(a, f)
        elif prev_save:
            prev_save(a, f)

    A["save_activity_opts"] = save_any

    @app.route("/t/<token>/live/<int:aid>", methods=["GET", "POST"])
    def learn_live(token, aid):
        enr = Enrollment.query.filter_by(token=token).first_or_404()
        a = Activity.query.get_or_404(aid)
        c = db.session.get(Course, a.course_id)
        prev = current_user.is_authenticated and current_user.role != "rh" and request.args.get("apercu") == "1"
        if a.kind != "live" or enr.status != "confirmed" or c.formation_id != enr.formation_id or \
                ((c.status != "published" or not a.visible) and not prev):
            abort(404)
        start, end = live_times(a)
        now = A["local_now"]().replace(tzinfo=None)
        is_open = bool(start and start - timedelta(minutes=15) <= now <= end)
        sid = a.opts().get("seance_id")
        att = Attendance.query.filter_by(seance_id=sid, enrollment_id=enr.id).first() if sid else None
        if request.method == "POST" and not prev:
            if not is_open or not a.url:
                flash(_("La classe virtuelle n'est pas ouverte."), "error")
                return redirect(request.path)
            if sid and not att:
                late = now > start + timedelta(minutes=10)
                att = Attendance(seance_id=sid, enrollment_id=enr.id, status="late" if late else "present",
                                 method="online", checkin_at=datetime.utcnow())
                db.session.add(att)
            if a.completion != "none":
                r = ActivityCompletion.query.filter_by(activity_id=a.id, enrollment_id=enr.id).first() or \
                    ActivityCompletion(activity_id=a.id, enrollment_id=enr.id)
                r.viewed_at = datetime.utcnow()
                r.completed_at = r.completed_at or datetime.utcnow()
                db.session.add(r)
            db.session.commit()
            return redirect(a.url)
        return render_template("learn_live.html", enr=enr, c=c, a=a, start=start, end=end, is_open=is_open, att=att, now=now,
                               preview=prev, q="?apercu=1" if prev else "",
                               secs=int((start - now).total_seconds()) if start and now < start else 0)

    # ── E-learning tracking for HR (and the company page) ────────────────────────────────────────
    def elearning_rows(scope_ids):
        studs = {s.id: s for s in Student.query.filter(Student.company_id.in_(scope_ids or [0]))}
        cos = {c.id: c.name for c in Company.query.filter(Company.id.in_(scope_ids or [0]))}
        enrs = Enrollment.query.filter(Enrollment.student_id.in_(list(studs) or [0]), Enrollment.status == "confirmed").all()
        byfo = {}
        for e in enrs:
            byfo.setdefault(e.formation_id, []).append(e)
        out = []
        for c in Course.query.filter(Course.formation_id.in_(list(byfo) or [0]), Course.status == "published").all():
            rows = []
            for e in sorted(byfo[c.formation_id], key=lambda e: e.student.last_name.lower()):
                pr = A["el_progress"](c, e)
                avg, _n = course_avg(c, e)
                aw = CourseAward.query.filter_by(course_id=c.id, enrollment_id=e.id).first()
                last = db.session.query(db.func.max(ActivityCompletion.viewed_at)).join(Activity, Activity.id == ActivityCompletion.activity_id) \
                    .filter(Activity.course_id == c.id, ActivityCompletion.enrollment_id == e.id).scalar()
                rows.append({"name": e.student.full_name, "company": cos.get(e.student.company_id, ""), "pct": pr["pct"],
                             "avg": avg, "award": aw, "last": last})
            n = len(rows)
            out.append({"course": c, "rows": rows, "avg_pct": round(sum(r["pct"] for r in rows) / n) if n else 0,
                        "done": sum(1 for r in rows if r["pct"] == 100), "badges": sum(1 for r in rows if r["award"])})
        return out

    A["elearning_rows"] = elearning_rows

    # ── Question generator (Claude if ANTHROPIC_API_KEY, otherwise built-in) ─────────────────────
    def page_text(c, ids=None):
        acts = [a for a in c.activities(True) if a.kind in ("page", "label", "file", "video", "url") and a.body]
        if ids:
            acts = [a for a in acts if a.id in ids]
        return acts

    def local_generate(text, n):
        """Cloze questions on the **bold** terms of the pages: short answer + MCQ with distractors."""
        terms = list(dict.fromkeys(t.strip() for t in re.findall(r"\*\*(.+?)\*\*", text) if 2 < len(t.strip()) < 60))
        plain = re.sub(r"[#>*`]", "", text)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?:])\s+|\n", re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)) if len(s.strip()) > 25]
        out, used = [], set()
        rnd = random.Random(len(text))
        for s in sentences:
            for t in re.findall(r"\*\*(.+?)\*\*", s):
                t = t.strip()
                if t in used or t not in terms:
                    continue
                used.add(t)
                stem = re.sub(r"\*\*" + re.escape(t) + r"\*\*", "______", s, count=1)
                stem = re.sub(r"[#>*`]|^\s*[-\d.)]+\s*", "", stem).strip()
                others = [x for x in terms if x != t and x.lower() not in t.lower()]
                if len(others) >= 3 and rnd.random() < .65:
                    opts = [{"t": t, "ok": True, "fb": ""}] + [{"t": x, "ok": False, "fb": ""} for x in rnd.sample(others, 3)]
                    rnd.shuffle(opts)
                    out.append({"qtype": "mcq", "text": f"Complétez : {stem}", "data": {"options": opts}})
                else:
                    out.append({"qtype": "short", "text": f"Complétez : {stem}", "data": {"answers": [t], "case": False}})
                break
            if len(out) >= n:
                break
        if len(out) < n:   # true/false on list items "X : Y"
            for m in re.finditer(r"^\s*[-*]\s+\*?\*?([^:\n*]{3,60})\*?\*?\s*:\s*(.{5,120})$", plain, re.M):
                out.append({"qtype": "tf", "text": f"{m.group(1).strip()} : {m.group(2).strip()}", "data": {"answer": True}})
                if len(out) >= n:
                    break
        return out[:n]

    def claude_generate(text, n, level):
        key = os.environ.get("ANTHROPIC_API_KEY")
        prompt = (f"Tu es ingénieur pédagogique. À partir du contenu de cours ci-dessous, rédige {n} questions de quiz en français, "
                  f"niveau {level}, variées (QCM à une bonne réponse, vrai/faux, réponse courte). Réponds uniquement avec un tableau JSON, "
                  'chaque élément : {"qtype":"mcq|tf|short","text":"…","options":[{"t":"…","ok":true}],"answer":true,'
                  '"answers":["…"],"feedback":"explication courte"}. Pour mcq : 4 options, une seule ok. Pour tf : answer. '
                  "Pour short : answers (1 à 3 formulations).\n\nCONTENU :\n" + text[:14000])
        body = json.dumps({"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 4000,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            res = json.loads(r.read())
        txt = "".join(b.get("text", "") for b in res.get("content", []))
        items = json.loads(txt[txt.find("["): txt.rfind("]") + 1])
        out = []
        for it in items:
            qt = it.get("qtype")
            if qt == "mcq" and it.get("options"):
                out.append({"qtype": "mcq", "text": it["text"], "data": {"options": [{"t": o["t"], "ok": bool(o.get("ok")), "fb": ""} for o in it["options"]]},
                            "feedback": it.get("feedback")})
            elif qt == "tf":
                out.append({"qtype": "tf", "text": it["text"], "data": {"answer": bool(it.get("answer"))}, "feedback": it.get("feedback")})
            elif qt == "short" and it.get("answers"):
                out.append({"qtype": "short", "text": it["text"], "data": {"answers": it["answers"], "case": False}, "feedback": it.get("feedback")})
        return out[:n]

    @app.route("/elearning/<int:cid>/questions/generer", methods=["GET", "POST"])
    @login_required
    def el_generate(cid):
        c = get_course(cid)
        acts = page_text(c)
        ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
        proposals, engine = None, None
        if request.method == "POST":
            f = request.form
            if f.get("action") == "add":
                items = json.loads(f.get("payload") or "[]")
                keep = {int(x) for x in f.getlist("keep") if x.isdigit()}
                cat = (f.get("category") or "").strip()[:80] or _("Générées")
                n = 0
                for i, it in enumerate(items):
                    if i in keep:
                        db.session.add(Question(course_id=c.id, category=cat, qtype=it["qtype"], text=it["text"][:4000],
                                                data=json.dumps(it["data"], ensure_ascii=False), feedback=it.get("feedback")))
                        n += 1
                log("questions_generated", f"{c.title} ({n})")
                db.session.commit()
                flash(_("{n} question(s) ajoutée(s) à la banque.", n=n), "success")
                return redirect(url_for("el_bank", cid=c.id))
            ids = {int(x) for x in f.getlist("src") if x.isdigit()}
            text = "\n\n".join(f"# {a.title}\n{a.body}" for a in page_text(c, ids))
            n = A["to_int"](f.get("n"), 1, 30) or 8
            if not text.strip():
                flash(_("Choisissez au moins une page avec du contenu."), "error")
                return redirect(request.path)
            engine = "local"
            if ai and f.get("engine") != "local":
                try:
                    proposals, engine = claude_generate(text, n, f.get("level") or "intermédiaire"), "claude"
                except Exception as e:  # noqa: BLE001
                    flash(_("L'IA n'a pas répondu ({e}) : générateur intégré utilisé.", e=str(e)[:80]), "error")
            if proposals is None:
                proposals = local_generate(text, n)
            if not proposals:
                flash(_("Pas assez de matière : mettez les notions clés en **gras** dans vos pages, ou configurez l'IA."), "error")
        return render_template("el_generate.html", c=c, acts=acts, ai=ai, proposals=proposals, engine=engine,
                               payload=json.dumps(proposals or [], ensure_ascii=False))

    A["_PUBLIC"].update({ep: (Enrollment, "token", "token") for ep in ("learn_award_pdf", "learn_live")})

    @app.context_processor
    def _ex_ctx():
        def award_for(c, enr):
            return CourseAward.query.filter_by(course_id=c.id, enrollment_id=enr.id).first()
        return {"award_for": award_for, "live_times": live_times}
