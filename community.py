"""Interaction (type Moodle) : annonces de cours, forums (discussion libre ou questions-réponses),
sondages / évaluation à chaud, calendrier (séances + échéances, export .ics) et notifications email.

Branché par app.py : community.register(app, globals())  (après elearning et assess)."""
import json
from datetime import date, datetime, timedelta

from flask import Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

import notify
from models import (Activity, ActivityCompletion, Announcement, Course, EmailLog, Enrollment, ForumPost, PollResponse,
                    Seance, Setting, Submission, db)

POLL_TYPES = ("choice", "multi", "scale", "text")


def register(app, A):  # noqa: C901
    _ = A["_"]
    log, today = A["log"], A["today"]
    icons = app.jinja_env.globals["KIND_ICONS"]
    icons.update({"forum": "chat-dots", "poll": "bar-chart-steps"})

    # ── helpers ───────────────────────────────────────────────────────────────────────────────────
    def get_course(cid):
        c = Course.query.get_or_404(cid)
        fo = c.formation
        if not (current_user.is_authenticated and current_user.role != "rh" and
                (current_user.can("manage") or (current_user.role == "formateur" and fo.trainer_id == current_user.id))):
            abort(403)
        return c

    def learner_ctx(token, aid):
        enr = Enrollment.query.filter_by(token=token).first_or_404()
        a = Activity.query.get_or_404(aid)
        c = db.session.get(Course, a.course_id)
        prev = current_user.is_authenticated and current_user.role != "rh" and request.args.get("apercu") == "1"
        if enr.status != "confirmed" or c.formation_id != enr.formation_id or ((c.status != "published" or not a.visible) and not prev):
            abort(404)
        if not prev and A["el_locked"](c, enr, a):
            flash(_("Cette ressource n'est pas encore accessible."), "error")
            abort(redirect(url_for("learn_course", token=token, cid=c.id)))
        return enr, a, c, prev

    def complete(enr, a):
        if a.completion == "none":
            return
        r = ActivityCompletion.query.filter_by(activity_id=a.id, enrollment_id=enr.id).first()
        if not r:
            r = ActivityCompletion(activity_id=a.id, enrollment_id=enr.id)
            db.session.add(r)
        r.viewed_at = datetime.utcnow()
        r.completed_at = r.completed_at or datetime.utcnow()

    def mail_learners(enrs, subject, title, paras, link_for, kind):
        """Email each learner (and nobody else) with his own personal link. Returns the number sent."""
        c = A["centre_settings"]()
        base, n = A["base_url"](), 0
        for e in enrs:
            to = (e.student.email or "").strip()
            if not to:
                continue
            html = notify.html_mail(c["centre_name"], title, [_("Bonjour {n},", n=e.student.first_name)] + paras,
                                    (_("Ouvrir"), base + link_for(e)))
            ok = notify.send_email(to, subject, "\n".join(paras) + "\n" + base + link_for(e), html, c["centre_name"])
            db.session.add(EmailLog(to=to, subject=subject[:200], kind=kind, ok=ok, error=(notify.LAST_ERROR["msg"] or "")[:300]))
            n += 1 if ok else 0
        return n

    # ── Announcements ─────────────────────────────────────────────────────────────────────────────
    @app.route("/elearning/<int:cid>/annonces", methods=["GET", "POST"])
    @login_required
    def el_announcements(cid):
        c = get_course(cid)
        if request.method == "POST":
            f = request.form
            if f.get("action") == "delete":
                an = Announcement.query.get_or_404(f.get("id", type=int))
                if an.course_id == c.id:
                    db.session.delete(an)
                    db.session.commit()
                return redirect(request.path)
            title = (f.get("title") or "").strip()[:200]
            if not title:
                flash(_("Le titre est obligatoire."), "error")
                return redirect(request.path)
            an = Announcement(course_id=c.id, title=title, body=(f.get("body") or "").strip() or None,
                              author=current_user.name, pinned=bool(f.get("pinned")))
            db.session.add(an)
            db.session.flush()
            sent = 0
            if f.get("email"):
                enrs = c.formation.enrollments.filter_by(status="confirmed").all()
                sent = mail_learners(enrs, f"{c.title} — {title}", title, [an.body or ""],
                                     lambda e: url_for("learn_course", token=e.token, cid=c.id), "announcement")
                an.emailed = sent
            log("announcement", f"{c.title} · {title}")
            db.session.commit()
            flash(_("Annonce publiée.") + (" " + _("{n} email(s) envoyé(s).", n=sent) if f.get("email") else ""), "success")
            return redirect(request.path)
        enrs = c.formation.enrollments.filter_by(status="confirmed").all()
        return render_template("el_announcements.html", c=c, items=Announcement.query.filter_by(course_id=c.id)
                               .order_by(Announcement.pinned.desc(), Announcement.created_at.desc()).all(),
                               enrs=enrs, with_email=sum(1 for e in enrs if e.student.email), mail_ok=A["mail_status"]()[0])

    # ── Forum ─────────────────────────────────────────────────────────────────────────────────────
    def threads(a):
        tops = ForumPost.query.filter_by(activity_id=a.id, parent_id=None).order_by(ForumPost.created_at.desc()).all()
        out = []
        for t in tops:
            reps = ForumPost.query.filter_by(activity_id=a.id, parent_id=t.id).order_by(ForumPost.created_at).all()
            out.append((t, reps))
        out.sort(key=lambda tr: max([tr[0].created_at] + [r.created_at for r in tr[1]]), reverse=True)
        return out

    def notify_reply(parent, reply, link):
        """Tell the author of a thread that someone answered (learner authors only, by email)."""
        if not parent.enrollment_id or parent.enrollment_id == reply.enrollment_id:
            return
        e = db.session.get(Enrollment, parent.enrollment_id)
        if e and e.student.email:
            mail_learners([e], _("Nouvelle réponse : {s}", s=parent.subject or ""), _("Nouvelle réponse dans le forum"),
                          [_("{a} a répondu à votre message « {s} ».", a=reply.author, s=parent.subject or "")],
                          lambda _e: link, "forum")

    def post_from_form(a, f, enr=None):
        body = (f.get("body") or "").strip()
        if not body:
            flash(_("Le message est vide."), "error")
            return None
        parent = ForumPost.query.get(f.get("parent_id", type=int)) if f.get("parent_id") else None
        if parent and parent.activity_id != a.id:
            abort(404)
        p = ForumPost(activity_id=a.id, parent_id=parent.id if parent else None, body=body[:8000],
                      subject=None if parent else ((f.get("subject") or "").strip()[:200] or body[:60]))
        if enr:
            p.enrollment_id, p.author = enr.id, enr.student.full_name
        else:
            p.user_id, p.author = current_user.id, current_user.name + " (" + (_("formateur") if current_user.role == "formateur" else _("équipe pédagogique")) + ")"
        db.session.add(p)
        db.session.flush()
        return p, parent

    @app.route("/elearning/<int:cid>/forum/<int:aid>", methods=["GET", "POST"])
    @login_required
    def el_forum(cid, aid):
        c = get_course(cid)
        a = Activity.query.get_or_404(aid)
        if a.course_id != c.id or a.kind != "forum":
            abort(404)
        if request.method == "POST":
            if request.form.get("action") == "delete":
                p = ForumPost.query.get_or_404(request.form.get("pid", type=int))
                if p.activity_id == a.id:
                    ForumPost.query.filter_by(parent_id=p.id).delete()
                    db.session.delete(p)
                    db.session.commit()
                return redirect(request.path)
            r = post_from_form(a, request.form)
            if r:
                p, parent = r
                if parent:
                    e = db.session.get(Enrollment, parent.enrollment_id) if parent.enrollment_id else None
                    if e:
                        notify_reply(parent, p, url_for("learn_forum", token=e.token, aid=a.id))
                db.session.commit()
            return redirect(request.path + (f"#p{r[0].parent_id or r[0].id}" if r else ""))
        return render_template("el_forum.html", c=c, a=a, threads=threads(a), staff=True)

    @app.route("/t/<token>/forum/<int:aid>", methods=["GET", "POST"])
    def learn_forum(token, aid):
        enr, a, c, prev = learner_ctx(token, aid)
        if a.kind != "forum":
            abort(404)
        mine = ForumPost.query.filter_by(activity_id=a.id, enrollment_id=enr.id).first()
        qa = a.opts().get("type") == "qa"
        if request.method == "POST" and not prev:
            r = post_from_form(a, request.form, enr)
            if r:
                p, parent = r
                if parent:
                    notify_reply(parent, p, url_for("learn_forum", token=db.session.get(Enrollment, parent.enrollment_id).token, aid=a.id)
                                 if parent.enrollment_id else "")
                complete(enr, a)
                db.session.commit()
                flash(_("Message publié."), "success")
            return redirect(request.path)
        hidden = qa and not mine and not prev
        return render_template("learn_forum.html", enr=enr, c=c, a=a, threads=[] if hidden else threads(a), hidden=hidden,
                               preview=prev, q="?apercu=1" if prev else "")

    # ── Poll / satisfaction survey ────────────────────────────────────────────────────────────────
    def poll_results(a):
        qs = a.opts().get("questions", [])
        res = [{"q": q, "counts": {}, "texts": [], "n": 0, "sum": 0} for q in qs]
        rs = PollResponse.query.filter_by(activity_id=a.id).all()
        for r in rs:
            ans = json.loads(r.answers or "{}")
            for i, q in enumerate(qs):
                v = ans.get(str(i))
                if v in (None, "", []):
                    continue
                R = res[i]
                R["n"] += 1
                if q["type"] == "text":
                    R["texts"].append(v)
                elif q["type"] == "multi":
                    for x in v:
                        R["counts"][x] = R["counts"].get(x, 0) + 1
                else:
                    R["counts"][str(v)] = R["counts"].get(str(v), 0) + 1
                    if q["type"] == "scale":
                        R["sum"] += int(v)
        return res, len(rs)

    @app.route("/elearning/<int:cid>/sondage/<int:aid>")
    @login_required
    def el_poll(cid, aid):
        c = get_course(cid)
        a = Activity.query.get_or_404(aid)
        if a.course_id != c.id or a.kind != "poll":
            abort(404)
        res, n = poll_results(a)
        total = c.formation.enrollments.filter_by(status="confirmed").count()
        if request.args.get("format") == "xlsx":
            qs = a.opts().get("questions", [])
            rows = [["Stagiaire"] + [q["text"] for q in qs]]
            for r in PollResponse.query.filter_by(activity_id=a.id):
                ans = json.loads(r.answers or "{}")
                who = "" if a.opts().get("anonymous") else db.session.get(Enrollment, r.enrollment_id).student.full_name
                rows.append([who] + [", ".join(v) if isinstance(v, list) else (v or "") for v in (ans.get(str(i)) for i in range(len(qs)))])
            return A["xlsx"](rows, f"sondage_{a.id}.xlsx", [26] + [30] * len(qs))
        return render_template("el_poll.html", c=c, a=a, res=res, n=n, total=total, o=a.opts())

    @app.route("/t/<token>/sondage/<int:aid>", methods=["GET", "POST"])
    def learn_poll(token, aid):
        enr, a, c, prev = learner_ctx(token, aid)
        if a.kind != "poll":
            abort(404)
        o = a.opts()
        mine = PollResponse.query.filter_by(activity_id=a.id, enrollment_id=enr.id).first()
        if request.method == "POST" and not prev:
            ans = {}
            for i, q in enumerate(o.get("questions", [])):
                k = f"q{i}"
                ans[str(i)] = request.form.getlist(k) if q["type"] == "multi" else (request.form.get(k) or "").strip()[:2000]
            if not mine:
                mine = PollResponse(activity_id=a.id, enrollment_id=enr.id)
                db.session.add(mine)
            mine.answers, mine.created_at = json.dumps(ans, ensure_ascii=False), datetime.utcnow()
            complete(enr, a)
            db.session.commit()
            flash(_("Merci, votre réponse est enregistrée."), "success")
            return redirect(request.path)
        res, n = poll_results(a) if (mine and o.get("show_results")) or prev else (None, 0)
        return render_template("learn_poll.html", enr=enr, c=c, a=a, o=o, mine=json.loads(mine.answers) if mine else None,
                               res=res, n=n, preview=prev, q="?apercu=1" if prev else "")

    def save_opts(a, f):
        if a.kind == "forum":
            a.set_opts({"type": "qa" if f.get("ftype") == "qa" else "general"})
            a.completion = "none" if f.get("completion") == "none" else "auto"
        elif a.kind == "poll":
            qs = []
            for i in range(10):
                t = (f.get(f"pq{i}") or "").strip()
                if not t:
                    continue
                ty = f.get(f"pt{i}") if f.get(f"pt{i}") in POLL_TYPES else "choice"
                opts = [x.strip() for x in (f.get(f"po{i}") or "").split(";") if x.strip()]
                if ty in ("choice", "multi") and len(opts) < 2:
                    opts = [_("Oui"), _("Non")]
                qs.append({"text": t[:300], "type": ty, "options": opts if ty in ("choice", "multi") else []})
            a.set_opts({"questions": qs, "anonymous": bool(f.get("anonymous")), "show_results": bool(f.get("show_results"))})
            a.completion = "none" if f.get("completion") == "none" else "auto"

    prev_save = A.get("save_activity_opts")

    def save_any(a, f):
        if a.kind in ("forum", "poll"):
            save_opts(a, f)
        elif prev_save:
            prev_save(a, f)

    A["save_activity_opts"] = save_any

    # ── Calendar ──────────────────────────────────────────────────────────────────────────────────
    def events_for(enrs_or_fos, d1, d2, learner_enr=None):
        """[{day, time, title, kind, url}] : séances, assignment deadlines, resources opening."""
        from models import Formation
        fos = enrs_or_fos
        ev = []
        fids = [f.id for f in fos] or [0]
        for s in Seance.query.filter(Seance.formation_id.in_(fids), Seance.day >= d1, Seance.day <= d2):
            ev.append({"day": s.day, "time": s.time_label or s.formation.default_time_label() or "", "title": s.formation.title,
                       "kind": "seance", "sub": s.topic or s.formation.location or "",
                       "url": url_for("seance_view", sid=s.id) if not learner_enr else None})
        courses = Course.query.filter(Course.formation_id.in_(fids)).all()
        for c in courses:
            if learner_enr and c.status != "published":
                continue
            for a in Activity.query.filter_by(course_id=c.id, visible=True):
                if a.kind == "assign" and a.opts().get("due"):
                    try:
                        dt = datetime.strptime(a.opts()["due"], "%Y-%m-%dT%H:%M")
                    except ValueError:
                        continue
                    if d1 <= dt.date() <= d2:
                        ev.append({"day": dt.date(), "time": dt.strftime("%H:%M"), "title": a.title, "kind": "due", "sub": c.title,
                                   "url": url_for("learn_eval", token=learner_enr.token, aid=a.id) if learner_enr
                                   else url_for("el_assign", cid=c.id, aid=a.id)})
                if a.available_from and d1 <= a.available_from <= d2:
                    ev.append({"day": a.available_from, "time": "", "title": a.title, "kind": "open", "sub": c.title,
                               "url": (url_for("learn_activity", token=learner_enr.token, aid=a.id) if learner_enr
                                       else url_for("el_activity", cid=c.id, aid=a.id))})
        ev.sort(key=lambda e: (e["day"], e["time"]))
        return ev

    def month_grid(y, m):
        first = date(y, m, 1)
        start = first - timedelta(days=(first.weekday() + 1) % 7)      # weeks start on Sunday (Algeria)
        return [[start + timedelta(days=w * 7 + d) for d in range(7)] for w in range(6)], first

    @app.route("/calendrier")
    @login_required
    def el_calendar():
        from models import Formation
        if current_user.role == "rh":
            abort(403)
        t = today()
        y, m = request.args.get("y", type=int) or t.year, request.args.get("m", type=int) or t.month
        weeks, first = month_grid(y, m)
        fos = Formation.query.filter(Formation.status != "draft").all()
        if current_user.role == "formateur":
            fos = [f for f in fos if f.trainer_id == current_user.id]
        ev = events_for(fos, weeks[0][0], weeks[-1][-1])
        by = {}
        for e in ev:
            by.setdefault(e["day"], []).append(e)
        pm = (first - timedelta(days=1))
        nm = (first + timedelta(days=32)).replace(day=1)
        return render_template("el_calendar.html", weeks=weeks, first=first, by=by, prev=(pm.year, pm.month), nxt=(nm.year, nm.month),
                               upcoming=[e for e in ev if e["day"] >= t][:12])

    @app.route("/t/<token>/calendrier")
    def learn_calendar(token):
        enr = Enrollment.query.filter_by(token=token).first_or_404()
        if enr.status != "confirmed":
            abort(404)
        t = today()
        ev = events_for([enr.formation], t - timedelta(days=7), t + timedelta(days=120), learner_enr=enr)
        return render_template("learn_calendar.html", enr=enr, ev=ev, today_=t)

    @app.route("/t/<token>/calendrier.ics")
    def learn_ics(token):
        enr = Enrollment.query.filter_by(token=token).first_or_404()
        t = today()
        ev = events_for([enr.formation], t - timedelta(days=30), t + timedelta(days=365), learner_enr=enr)
        centre = Setting.get("centre_name") or "Formation"

        def esc(s):
            return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AcademieProLearning//FR", "CALSCALE:GREGORIAN",
                 f"X-WR-CALNAME:{esc(centre)} — {esc(enr.formation.title)}"]
        for i, e in enumerate(ev):
            import re
            m = re.findall(r"(\d{1,2})[:h](\d{2})", e["time"] or "")
            lines.append("BEGIN:VEVENT")
            lines.append(f"UID:{enr.token[:10]}-{i}-{e['day']:%Y%m%d}@prolearning")
            if m:
                h1, m1 = m[0]
                h2, m2 = m[1] if len(m) > 1 else (str(int(h1) + 1), m1)
                lines += [f"DTSTART:{e['day']:%Y%m%d}T{int(h1):02d}{m1}00", f"DTEND:{e['day']:%Y%m%d}T{int(h2):02d}{m2}00"]
            else:
                lines += [f"DTSTART;VALUE=DATE:{e['day']:%Y%m%d}", f"DTEND;VALUE=DATE:{e['day'] + timedelta(days=1):%Y%m%d}"]
            prefix = {"due": "📌 ", "open": "🔓 "}.get(e["kind"], "")
            lines += [f"SUMMARY:{esc(prefix + e['title'])}", f"DESCRIPTION:{esc(e['sub'])}", "END:VEVENT"]
        lines.append("END:VCALENDAR")
        return Response("\r\n".join(lines) + "\r\n", mimetype="text/calendar",
                        headers={"Content-Disposition": f"inline; filename=calendrier_{enr.formation.slug[:30]}.ics"})

    # ── Notifications : deadline reminder (hourly job, 24 h before) and graded assignment ──────────
    def _run_due_reminders():
        if not (notify.current_cfg() or {}).get("from_email"):  # keep the reminder for when emails work
            return 0
        now = A["local_now"]().replace(tzinfo=None)
        sent = 0
        for a in Activity.query.filter_by(kind="assign", visible=True).all():
            o = a.opts()
            try:
                due = datetime.strptime(o.get("due") or "", "%Y-%m-%dT%H:%M")
            except ValueError:
                continue
            if not (timedelta(hours=0) < due - now <= timedelta(hours=24)) or o.get("reminded"):
                continue
            c = db.session.get(Course, a.course_id)
            if c.status != "published":
                continue
            done = {s.enrollment_id for s in Submission.query.filter(Submission.activity_id == a.id, Submission.submitted_at.isnot(None))}
            enrs = [e for e in c.formation.enrollments.filter_by(status="confirmed") if e.id not in done]
            sent += mail_learners(enrs, _("Rappel : « {t} » à rendre demain", t=a.title), _("Devoir à rendre"),
                                  [_("Le devoir « {t} » est à rendre avant le {d}.", t=a.title, d=due.strftime("%d/%m/%Y %H:%M"))],
                                  lambda e: url_for("learn_eval", token=e.token, aid=a.id), "due_reminder")
            o["reminded"] = True
            a.set_opts(o)
            db.session.commit()
        return sent

    A["run_due_reminders"] = A["_each_school"](_run_due_reminders)

    def notify_graded(s, a):
        e = db.session.get(Enrollment, s.enrollment_id)
        if e and e.student.email and s.grade is not None:
            mx = a.opts().get("max_points") or 20
            mail_learners([e], _("Votre devoir « {t} » est corrigé", t=a.title), _("Devoir corrigé"),
                          [_("Note : {g} / {m}", g=("%g" % s.grade).replace(".", ","), m=mx)] + ([s.feedback] if s.feedback else []),
                          lambda _e: url_for("learn_eval", token=e.token, aid=a.id), "graded")

    A["notify_graded"] = notify_graded

    # Learner side: forum / poll activities go to their own pages
    A["_PUBLIC"].update({ep: (Enrollment, "token", "token") for ep in ("learn_forum", "learn_poll", "learn_calendar", "learn_ics")})

    @app.context_processor
    def _com_ctx():
        def course_news(c):
            return Announcement.query.filter_by(course_id=c.id).order_by(Announcement.pinned.desc(),
                                                                           Announcement.created_at.desc()).limit(3).all()
        return {"course_news": course_news}
