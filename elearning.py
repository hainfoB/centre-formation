"""E-learning (inspiré de Moodle) : cours en ligne rattachés à une formation, sections, ressources
(page, fichier, vidéo, lien, étiquette), achèvement, accès progressif et suivi de la progression.

Les apprenants n'ont pas de compte : ils accèdent à leurs cours par leur lien personnel /t/<token>.
Branché par app.py : elearning.register(app, globals())."""
import html as _html
import io
import re
from datetime import datetime
from functools import wraps
from urllib.parse import parse_qs, urlparse

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from markupsafe import Markup

from models import (ACTIVITY_KINDS, Activity, ActivityCompletion, Course, CourseSection, Enrollment, Formation,
                    StoredFile, db)

MAX_FILE = 20 * 1024 * 1024
KIND_ICONS = {"page": "file-text", "file": "file-earmark-arrow-down", "video": "play-btn", "url": "link-45deg",
              "label": "tag", "quiz": "patch-question", "assign": "clipboard-check"}


# ── Simple, safe markdown (titles, bold, italic, lists, links, images by URL) ─────────────────────
def md(text):
    if not text:
        return Markup("")
    out, lst = [], None
    for raw in text.replace("\r", "").split("\n"):
        line = _html.escape(raw.rstrip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<!\*)\*(?!\s)(.+?)\*", r"<i>\1</i>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        line = re.sub(r"!\[([^\]]*)\]\((https?://[^\s)]+)\)", r'<img src="\2" alt="\1" loading="lazy">', line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', line)
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        li = re.match(r"^\s*(?:[-*]|(\d+)[.)])\s+(.*)", line)
        if li:
            tag = "ol" if li.group(1) else "ul"
            if lst != tag:
                if lst:
                    out.append(f"</{lst}>")
                out.append(f"<{tag}>")
                lst = tag
            out.append(f"<li>{li.group(2)}</li>")
            continue
        if lst:
            out.append(f"</{lst}>")
            lst = None
        if m:
            n = len(m.group(1)) + 1
            out.append(f"<h{n}>{m.group(2)}</h{n}>")
        elif line.startswith("&gt; "):
            out.append(f'<blockquote>{line[5:]}</blockquote>')
        elif line.strip():
            out.append(f"<p>{line}</p>")
    if lst:
        out.append(f"</{lst}>")
    return Markup("\n".join(out))


def video_embed(url):
    """(kind, src) for YouTube, Vimeo, Google Drive or a direct video file."""
    if not url:
        return None, None
    u = urlparse(url)
    host = (u.netloc or "").lower().replace("www.", "")
    if host in ("youtube.com", "m.youtube.com") and u.path == "/watch":
        vid = parse_qs(u.query).get("v", [""])[0]
        return "iframe", f"https://www.youtube-nocookie.com/embed/{vid}"
    if host == "youtu.be":
        return "iframe", f"https://www.youtube-nocookie.com/embed/{u.path.strip('/')}"
    if host in ("youtube.com",) and u.path.startswith(("/embed/", "/shorts/")):
        return "iframe", "https://www.youtube-nocookie.com/embed/" + u.path.split("/")[2]
    if host == "vimeo.com" and u.path.strip("/").isdigit():
        return "iframe", f"https://player.vimeo.com/video/{u.path.strip('/')}"
    if host == "drive.google.com" and "/file/d/" in u.path:
        return "iframe", "https://drive.google.com/file/d/" + u.path.split("/file/d/")[1].split("/")[0] + "/preview"
    if re.search(r"\.(mp4|webm|ogg)$", u.path, re.I):
        return "video", url
    return "link", url


def register(app, A):  # noqa: C901
    _ = A["_"]
    today, log = A["today"], A["log"]
    A["_PUBLIC"].update({ep: (Enrollment, "token", "token") for ep in
                         ("learn_home", "learn_course", "learn_activity", "learn_file")})
    app.jinja_env.filters["md"] = md
    app.jinja_env.globals["video_embed"] = video_embed
    app.jinja_env.globals["KIND_ICONS"] = KIND_ICONS

    # ── Progress ──────────────────────────────────────────────────────────────────────────────────
    def tracked(acts):
        return [a for a in acts if a.visible and a.kind != "label" and a.completion != "none"]

    def completions(enr_ids, course):
        rows = ActivityCompletion.query.join(Activity, Activity.id == ActivityCompletion.activity_id) \
            .filter(Activity.course_id == course.id, ActivityCompletion.enrollment_id.in_(enr_ids or [0])).all()
        out = {}
        for r in rows:
            out.setdefault(r.enrollment_id, {})[r.activity_id] = r
        return out

    def progress(course, enr, comp=None):
        acts = course.activities(visible_only=True)
        tr = tracked(acts)
        comp = comp if comp is not None else completions([enr.id], course).get(enr.id, {})
        done = {aid for aid, r in comp.items() if r.completed_at}
        n = sum(1 for a in tr if a.id in done)
        return {"done": n, "total": len(tr), "pct": round(n * 100 / len(tr)) if tr else 0, "done_ids": done,
                "viewed": set(comp), "acts": acts}

    def section_states(course, pr):
        """[(section, activities, locked, complete)] for the learner view."""
        out, prev_ok = [], True
        for s in course.sections.filter_by(visible=True).all():
            acts = [a for a in pr["acts"] if a.section_id == s.id]
            tr = tracked(acts)
            complete = all(a.id in pr["done_ids"] for a in tr)
            locked = course.sequential and not prev_ok
            out.append((s, acts, locked, complete))
            prev_ok = prev_ok and complete
        return out

    A["el_progress"] = progress

    # ── Staff access ──────────────────────────────────────────────────────────────────────────────
    def can_edit(course_or_fo):
        fo = course_or_fo.formation if isinstance(course_or_fo, Course) else course_or_fo
        return current_user.can("manage") or (current_user.role == "formateur" and fo.trainer_id == current_user.id)

    def staff_required(fn):
        @wraps(fn)
        @login_required
        def inner(*a, **kw):
            if current_user.role == "rh" or not (current_user.can("manage") or current_user.role == "formateur"):
                abort(403)
            return fn(*a, **kw)
        return inner

    def get_course(cid):
        c = Course.query.get_or_404(cid)
        if not can_edit(c):
            abort(403)
        return c

    def save_upload(fs):
        if not fs or not fs.filename:
            return None
        data = fs.read()
        if len(data) > MAX_FILE:
            flash(_("Fichier trop volumineux (20 Mo maximum)."), "error")
            return None
        sf = StoredFile(name=fs.filename[-200:], mime=fs.mimetype or "application/octet-stream", size=len(data), data=data)
        db.session.add(sf)
        db.session.flush()
        return sf

    @app.route("/elearning", methods=["GET", "POST"])
    @staff_required
    def el_courses():
        fos = Formation.query.order_by(Formation.starts_on.desc().nullslast()).all()
        if current_user.role == "formateur":
            fos = [f for f in fos if f.trainer_id == current_user.id]
        if request.method == "POST":
            fo = Formation.query.get_or_404(request.form.get("formation_id", type=int))
            if not can_edit(fo):
                abort(403)
            title = (request.form.get("title") or fo.title).strip()[:200]
            c = Course(formation_id=fo.id, title=title, summary=(request.form.get("summary") or "").strip() or None)
            db.session.add(c)
            db.session.flush()
            for i, t in enumerate((_("Bienvenue et présentation"), _("Module 1"), _("Module 2"), _("Évaluation finale"))):
                db.session.add(CourseSection(course_id=c.id, position=i, title=t))
            log("course_created", title)
            db.session.commit()
            flash(_("Cours créé avec 4 sections. Ajoutez maintenant vos ressources."), "success")
            return redirect(url_for("el_course", cid=c.id))
        fids = [f.id for f in fos] or [0]
        courses = Course.query.filter(Course.formation_id.in_(fids)).order_by(Course.created_at.desc()).all()
        stats = {}
        for c in courses:
            enrs = c.formation.enrollments.filter_by(status="confirmed").all()
            comp = completions([e.id for e in enrs], c)
            prs = [progress(c, e, comp.get(e.id, {}))["pct"] for e in enrs]
            stats[c.id] = {"learners": len(enrs), "avg": round(sum(prs) / len(prs)) if prs else 0,
                           "done": sum(1 for p in prs if p == 100), "acts": len(tracked(c.activities(True))),
                           "started": sum(1 for e in enrs if comp.get(e.id))}
        return render_template("el_courses.html", courses=courses, stats=stats, fos=fos)

    @app.route("/elearning/<int:cid>", methods=["GET", "POST"])
    @staff_required
    def el_course(cid):
        c = get_course(cid)
        if request.method == "POST":
            f, act = request.form, request.form.get("action")
            if act == "save_course":
                c.title = (f.get("title") or c.title).strip()[:200]
                c.summary = (f.get("summary") or "").strip() or None
                c.sequential = bool(f.get("sequential"))
                if f.get("status") in ("draft", "published"):
                    c.status = f.get("status")
                flash(_("Cours enregistré."), "success")
            elif act == "publish":
                c.status = "draft" if c.status == "published" else "published"
                flash(_("Cours publié : les stagiaires le voient dans leur espace.") if c.status == "published"
                      else _("Cours repassé en brouillon."), "success")
            elif act == "add_section":
                n = c.sections.count()
                db.session.add(CourseSection(course_id=c.id, position=n, title=(f.get("title") or _("Nouvelle section")).strip()[:200]))
            elif act in ("edit_section", "del_section", "up_section", "down_section", "toggle_section"):
                s = CourseSection.query.get_or_404(f.get("section_id", type=int))
                if s.course_id != c.id:
                    abort(404)
                secs = c.sections.all()
                if act == "edit_section":
                    s.title = (f.get("title") or s.title).strip()[:200]
                    s.summary = (f.get("summary") or "").strip() or None
                elif act == "del_section":
                    if s.activities.count():
                        flash(_("Videz d'abord la section."), "error")
                    else:
                        db.session.delete(s)
                elif act == "toggle_section":
                    s.visible = not s.visible
                else:
                    i = secs.index(s)
                    j = i - 1 if act == "up_section" else i + 1
                    if 0 <= j < len(secs):
                        secs[i], secs[j] = secs[j], secs[i]
                    for k, x in enumerate(secs):
                        x.position = k
            elif act in ("del_activity", "up_activity", "down_activity", "toggle_activity"):
                a = Activity.query.get_or_404(f.get("activity_id", type=int))
                if a.course_id != c.id:
                    abort(404)
                if act == "del_activity":
                    ActivityCompletion.query.filter_by(activity_id=a.id).delete()
                    db.session.delete(a)
                elif act == "toggle_activity":
                    a.visible = not a.visible
                else:
                    acts = a.section.activities.all()
                    i = acts.index(a)
                    j = i - 1 if act == "up_activity" else i + 1
                    if 0 <= j < len(acts):
                        acts[i], acts[j] = acts[j], acts[i]
                    for k, x in enumerate(acts):
                        x.position = k
            elif act == "delete_course":
                ids = [a.id for a in c.activities()]
                ActivityCompletion.query.filter(ActivityCompletion.activity_id.in_(ids or [0])).delete(synchronize_session=False)
                db.session.delete(c)
                log("course_deleted", c.title)
                db.session.commit()
                flash(_("Cours supprimé."), "success")
                return redirect(url_for("el_courses"))
            db.session.commit()
            return redirect(url_for("el_course", cid=c.id) + (f"#s{f.get('section_id')}" if f.get("section_id") else ""))
        enrs = c.formation.enrollments.filter_by(status="confirmed").all()
        sample = enrs[0] if enrs else None
        return render_template("el_course.html", c=c, sections=c.sections.all(), learners=len(enrs), sample=sample,
                               kinds=ACTIVITY_KINDS)

    @app.route("/elearning/<int:cid>/activite", methods=["GET", "POST"])
    @app.route("/elearning/<int:cid>/activite/<int:aid>", methods=["GET", "POST"])
    @staff_required
    def el_activity(cid, aid=None):
        c = get_course(cid)
        a = Activity.query.get_or_404(aid) if aid else None
        if a and a.course_id != c.id:
            abort(404)
        kind = a.kind if a else (request.args.get("kind") if request.args.get("kind") in ACTIVITY_KINDS else "page")
        sid = a.section_id if a else request.args.get("section", type=int)
        if request.method == "POST":
            f = request.form
            sec = CourseSection.query.get_or_404(f.get("section_id", type=int) or sid)
            if sec.course_id != c.id:
                abort(404)
            if not a:
                a = Activity(course_id=c.id, kind=kind, position=sec.activities.count())
                db.session.add(a)
            a.section_id = sec.id
            a.title = (f.get("title") or "").strip()[:200] or _("Sans titre")
            a.body = (f.get("body") or "").strip() or None
            a.url = (f.get("url") or "").strip()[:500] or None
            a.duration_min = A["to_int"](f.get("duration_min"), 0, 10000) or None
            a.completion = f.get("completion") if f.get("completion") in ("view", "manual", "none", "auto") else "view"
            if a.kind == "label":
                a.completion = "none"
            if a.kind in ("quiz", "assign", "forum", "poll", "live") and A.get("save_activity_opts"):
                A["save_activity_opts"](a, f)
            a.available_from = A["parse_date"](f.get("available_from"))
            a.visible = not f.get("hidden")
            if a.kind == "file":
                sf = save_upload(request.files.get("file"))
                if sf:
                    a.file_id = sf.id
                elif not a.file_id:
                    db.session.rollback()
                    flash(_("Choisissez un fichier."), "error")
                    return redirect(request.url)
            if a.kind in ("video", "url", "live") and not a.url and not (a.kind == "video" and a.file_id):
                db.session.rollback()
                flash(_("Indiquez l'adresse (URL)."), "error")
                return redirect(request.url)
            log("activity_saved", f"{c.title} · {a.title}")
            db.session.commit()
            flash(_("Ressource enregistrée."), "success")
            return redirect(url_for("el_course", cid=c.id) + f"#s{a.section_id}")
        return render_template("el_activity_form.html", c=c, a=a, kind=kind, sid=sid, sections=c.sections.all(),
                               o=a.opts() if a else {}, bank=A["bank_for"](c) if kind == "quiz" else [])

    @app.route("/elearning/<int:cid>/suivi")
    @staff_required
    def el_progress(cid):
        c = get_course(cid)
        enrs = sorted(c.formation.enrollments.filter_by(status="confirmed").all(),
                      key=lambda e: (e.student.last_name.lower(), e.student.first_name.lower()))
        comp = completions([e.id for e in enrs], c)
        acts = tracked(c.activities(True))
        rows = []
        for e in enrs:
            cm = comp.get(e.id, {})
            pr = progress(c, e, cm)
            last = max((r.viewed_at for r in cm.values()), default=None)
            rows.append({"enr": e, "pr": pr, "cm": cm, "last": last})
        if request.args.get("format") == "xlsx":
            hdr = ["Stagiaire", "Entreprise"] + [a.title for a in acts] + ["Progression %", "Dernier accès"]
            data = [[r["enr"].student.full_name, A["db"].session.get(A["Company"], r["enr"].student.company_id).name
                     if r["enr"].student.company_id else ""] +
                    ["✓" if a.id in r["pr"]["done_ids"] else ("vu" if a.id in r["cm"] else "") for a in acts] +
                    [r["pr"]["pct"], r["last"].strftime("%d/%m/%Y %H:%M") if r["last"] else ""] for r in rows]
            return A["xlsx"]([hdr] + data, f"progression_{c.id}.xlsx", [28, 24] + [14] * len(acts) + [14, 18])
        return render_template("el_progress.html", c=c, rows=rows, acts=acts)

    @app.route("/elearning/fichier/<int:fid>")
    @staff_required
    def el_file(fid):
        sf = StoredFile.query.get_or_404(fid)
        return send_file(io.BytesIO(sf.data), mimetype=sf.mime, download_name=sf.name,
                         as_attachment=not (sf.mime or "").startswith(("application/pdf", "image/", "video/")))

    # ── Learner side (personal link, no account) ─────────────────────────────────────────────────
    def learner(token):
        enr = Enrollment.query.filter_by(token=token).first_or_404()
        if enr.status != "confirmed":
            abort(404)
        return enr

    def learner_course(enr, cid, preview=False):
        c = Course.query.get_or_404(cid)
        if c.formation_id != enr.formation_id or (c.status != "published" and not preview):
            abort(404)
        return c

    def is_preview():
        return current_user.is_authenticated and current_user.role != "rh" and request.args.get("apercu") == "1"

    @app.route("/t/<token>/cours")
    def learn_home(token):
        enr = learner(token)
        courses = Course.query.filter_by(formation_id=enr.formation_id, status="published").all()
        if len(courses) == 1:
            return redirect(url_for("learn_course", token=token, cid=courses[0].id))
        return render_template("learn_home.html", enr=enr, courses=[(c, progress(c, enr)) for c in courses])

    @app.route("/t/<token>/cours/<int:cid>")
    def learn_course(token, cid):
        enr = learner(token)
        prev = is_preview()
        c = learner_course(enr, cid, prev)
        pr = progress(c, enr)
        secs = section_states(c, pr)
        nxt = next((a for s, acts, locked, _x in secs if not locked for a in acts
                    if a.kind != "label" and a.completion != "none" and a.id not in pr["done_ids"] and (not a.available_from or a.available_from <= today())), None)
        award = A["check_award"](c, enr) if A.get("check_award") and not prev else None
        return render_template("learn_course.html", enr=enr, c=c, pr=pr, secs=secs, nxt=nxt, preview=prev, today_=today(), award=award)

    def ordered_open(c, enr, pr):
        secs = section_states(c, pr)
        flat, locked_ids = [], set()
        for s, acts, locked, _x in secs:
            for a in acts:
                if a.kind == "label":
                    continue
                if locked or (a.available_from and a.available_from > today()):
                    locked_ids.add(a.id)
                flat.append(a)
        return flat, locked_ids

    def is_locked(c, enr, a):
        return a.id in ordered_open(c, enr, progress(c, enr))[1]

    A["el_locked"] = is_locked

    def mark(enr, a, complete):
        r = ActivityCompletion.query.filter_by(activity_id=a.id, enrollment_id=enr.id).first()
        if not r:
            r = ActivityCompletion(activity_id=a.id, enrollment_id=enr.id)
            db.session.add(r)
        r.viewed_at = datetime.utcnow()
        if complete and not r.completed_at:
            r.completed_at = datetime.utcnow()
        elif complete is False:
            r.completed_at = None
        return r

    @app.route("/t/<token>/a/<int:aid>", methods=["GET", "POST"])
    def learn_activity(token, aid):
        enr = learner(token)
        a = Activity.query.get_or_404(aid)
        prev = is_preview()
        c = learner_course(enr, a.course_id, prev)
        if not a.visible and not prev:
            abort(404)
        pr = progress(c, enr)
        flat, locked = ordered_open(c, enr, pr)
        if a.id in locked and not prev:
            flash(_("Cette ressource n'est pas encore accessible."), "error")
            return redirect(url_for("learn_course", token=token, cid=c.id))
        if a.kind in ("quiz", "assign"):
            return redirect(url_for("learn_eval", token=token, aid=a.id, apercu=1 if prev else None))
        if a.kind == "live":
            return redirect(url_for("learn_live", token=token, aid=a.id, apercu=1 if prev else None))
        if a.kind in ("forum", "poll"):
            return redirect(url_for("learn_forum" if a.kind == "forum" else "learn_poll", token=token, aid=a.id,
                                    apercu=1 if prev else None))
        if not prev:
            if request.method == "POST":
                mark(enr, a, request.form.get("done") == "1")
                db.session.commit()
                nxt = request.form.get("next")
                return redirect(nxt if nxt and nxt.startswith("/t/") else url_for("learn_activity", token=token, aid=a.id))
            mark(enr, a, True if a.completion == "view" else None)
            db.session.commit()
            pr = progress(c, enr)
        i = flat.index(a) if a in flat else -1
        prv = flat[i - 1] if i > 0 else None
        nxt = flat[i + 1] if 0 <= i < len(flat) - 1 else None
        return render_template("learn_activity.html", enr=enr, c=c, a=a, pr=pr, prv=prv, nxt=nxt, preview=prev,
                               done=a.id in pr["done_ids"], q="?apercu=1" if prev else "")

    @app.route("/t/<token>/fichier/<int:aid>")
    def learn_file(token, aid):
        enr = learner(token)
        a = Activity.query.get_or_404(aid)
        c = learner_course(enr, a.course_id, is_preview())
        if not a.file:
            abort(404)
        sf = a.file
        return send_file(io.BytesIO(sf.data), mimetype=sf.mime, download_name=sf.name,
                         as_attachment=request.args.get("dl") == "1" or not (sf.mime or "").startswith(("application/pdf", "image/", "video/")))

    @app.context_processor
    def _el_ctx():
        return {"el_courses_for": lambda enr: [(c, progress(c, enr)) for c in
                                               Course.query.filter_by(formation_id=enr.formation_id, status="published").all()]}
