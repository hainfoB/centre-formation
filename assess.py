"""Évaluation (type Moodle) : banque de questions (QCM, choix multiples, vrai/faux, réponse courte,
numérique, appariement), import GIFT, quiz (tirage, minuterie, tentatives, seuil de réussite, relecture),
devoirs à rendre (texte / fichier, date limite, note et commentaire) et carnet de notes.

Branché par app.py : assess.register(app, globals())  (après elearning)."""
import io
import json
import random
import re
import unicodedata
from datetime import datetime, timedelta

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from models import (QTYPES, Activity, ActivityCompletion, Course, Enrollment, Question, QuizAttempt, StoredFile,
                    Submission, db)


def _norm(s, case=False):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode() if not re.search(r"[؀-ۿ]", s or "") else (s or "")
    s = re.sub(r"\s+", " ", s.strip())
    return s if case else s.lower()


def grade_question(q, ans):
    """(earned points, fraction 0..1). ans is the raw answer stored for this question."""
    d, pts = q.d(), q.points or 1
    frac = 0.0
    try:
        if q.qtype == "mcq":
            opts = d.get("options", [])
            frac = 1.0 if ans not in (None, "") and opts[int(ans)].get("ok") else 0.0
        elif q.qtype == "multi":
            opts = d.get("options", [])
            sel = {int(x) for x in (ans or [])}
            good = {i for i, o in enumerate(opts) if o.get("ok")}
            if good:
                frac = max(0.0, (len(sel & good) - len(sel - good)) / len(good))
        elif q.qtype == "tf":
            frac = 1.0 if ans in ("1", "0") and (ans == "1") == bool(d.get("answer")) else 0.0
        elif q.qtype == "short":
            given = _norm(ans, d.get("case"))
            for a in d.get("answers", []):
                pat = "^" + re.escape(_norm(a, d.get("case"))).replace(r"\*", ".*") + "$"
                if given and re.match(pat, given):
                    frac = 1.0
                    break
        elif q.qtype == "numeric":
            v = float(str(ans).replace(",", ".").strip())
            frac = 1.0 if abs(v - float(d.get("answer", 0))) <= float(d.get("tol") or 0) else 0.0
        elif q.qtype == "match":
            pairs = d.get("pairs", [])
            ans = ans or {}
            ok = sum(1 for i, (_l, r) in enumerate(pairs) if _norm(ans.get(str(i), "")) == _norm(r))
            frac = ok / len(pairs) if pairs else 0.0
    except (ValueError, IndexError, TypeError):
        frac = 0.0
    return round(pts * frac, 2), frac


def correct_text(q):
    d = q.d()
    if q.qtype in ("mcq", "multi"):
        return " · ".join(o["t"] for o in d.get("options", []) if o.get("ok"))
    if q.qtype == "tf":
        return "Vrai" if d.get("answer") else "Faux"
    if q.qtype == "short":
        return " / ".join(d.get("answers", []))
    if q.qtype == "numeric":
        return f'{d.get("answer")}' + (f' (± {d.get("tol")})' if d.get("tol") else "")
    if q.qtype == "match":
        return " · ".join(f"{l} → {r}" for l, r in d.get("pairs", []))
    return ""


# ── GIFT import (Moodle format, common subset) ────────────────────────────────────────────────────
def parse_gift(text):
    """Returns [(category, qtype, text, data, feedback)]. Supports MCQ, multi (%weights), T/F, short,
    numeric (#v:tol), matching (a -> b), $CATEGORY, ::titles::, // comments, #feedback."""
    out, cat = [], None
    text = re.sub(r"(?m)^\s*//.*$", "", text.replace("\r", ""))
    for block in re.split(r"\n\s*\n", text):
        b = block.strip()
        if not b:
            continue
        m = re.match(r"\$CATEGORY:\s*(.+)", b)
        if m:
            cat = m.group(1).strip().split("/")[-1]
            b = b[m.end():].strip()
            if not b:
                continue
        b = re.sub(r"^::.*?::", "", b).strip()
        m = re.search(r"\{(.*)\}", b, re.S)
        if not m:
            continue
        qtext = (b[:m.start()] + (" ___ " if b[m.end():].strip() else "") + b[m.end():]).strip()
        body = m.group(1).strip()
        fb = None
        if body.upper() in ("T", "TRUE", "F", "FALSE") or re.match(r"^(T|TRUE|F|FALSE)\s*#", body, re.I):
            v = body.split("#")[0].strip().upper()
            out.append((cat, "tf", qtext, {"answer": v in ("T", "TRUE")}, None))
            continue
        if body.startswith("#"):
            nm = re.match(r"#\s*(-?[\d.,]+)(?::([\d.,]+))?", body)
            if nm:
                out.append((cat, "numeric", qtext, {"answer": float(nm.group(1).replace(",", ".")),
                                                    "tol": float((nm.group(2) or "0").replace(",", "."))}, None))
            continue
        parts = re.findall(r"([=~])((?:\\.|[^=~])*)", body)
        if parts and all(p[0] == "=" for p in parts) and all("->" in p[1] for p in parts):
            pairs = [[x.strip() for x in p[1].split("->", 1)] for p in parts]
            out.append((cat, "match", qtext, {"pairs": pairs}, None))
            continue
        opts, weights = [], []
        for sign, raw in parts:
            raw = raw.strip()
            ofb = ""
            if "#" in raw:
                raw, ofb = raw.split("#", 1)
            w = None
            wm = re.match(r"%(-?[\d.]+)%(.*)", raw.strip())
            if wm:
                w, raw = float(wm.group(1)), wm.group(2)
            opts.append({"t": raw.strip().replace("\\=", "=").replace("\\~", "~"), "ok": sign == "=" or (w or 0) > 0,
                         "fb": ofb.strip()})
            weights.append(w)
        if not opts:
            continue
        if all(o["ok"] for o in opts) and all(p[0] == "=" for p in parts):
            out.append((cat, "short", qtext, {"answers": [o["t"] for o in opts]}, fb))
        elif sum(1 for o in opts if o["ok"]) > 1 or any(w is not None for w in weights):
            out.append((cat, "multi", qtext, {"options": opts}, fb))
        else:
            out.append((cat, "mcq", qtext, {"options": opts}, fb))
    return out


def register(app, A):  # noqa: C901
    _ = A["_"]
    log, today = A["log"], A["today"]
    QLAB = {"mcq": "QCM — une réponse", "multi": "Choix multiples", "tf": "Vrai / Faux",
            "short": "Réponse courte", "numeric": "Numérique", "match": "Appariement"}   # translated in templates
    app.jinja_env.globals["QLAB"] = QLAB
    app.jinja_env.filters["num"] = lambda v: ("" if v is None else ("%g" % round(float(v), 2)).replace(".", ","))

    def get_course(cid):
        c = Course.query.get_or_404(cid)
        fo = c.formation
        if not (current_user.is_authenticated and current_user.role != "rh" and
                (current_user.can("manage") or (current_user.role == "formateur" and fo.trainer_id == current_user.id))):
            abort(403)
        return c

    def staff(fn):
        from functools import wraps

        from flask_login import login_required

        @wraps(fn)
        @login_required
        def inner(*a, **kw):
            return fn(*a, **kw)
        return inner

    # ── Item scores (for gradebook, progress and learner) ────────────────────────────────────────
    def quiz_result(a, enr_id):
        o = a.opts()
        ats = QuizAttempt.query.filter_by(activity_id=a.id, enrollment_id=enr_id).filter(QuizAttempt.finished_at.isnot(None)) \
            .order_by(QuizAttempt.number).all()
        if not ats:
            return None
        pct = ats[-1].pct if o.get("grade_method") == "last" else max(x.pct or 0 for x in ats)
        return round(pct or 0, 1)

    def assign_result(a, enr_id):
        s = Submission.query.filter_by(activity_id=a.id, enrollment_id=enr_id).first()
        if not s or s.grade is None:
            return None
        mx = float(a.opts().get("max_points") or 20)
        return round(s.grade * 100 / mx, 1) if mx else None

    def item_pct(a, enr_id):
        return quiz_result(a, enr_id) if a.kind == "quiz" else assign_result(a, enr_id) if a.kind == "assign" else None

    def complete(enr, a):
        r = ActivityCompletion.query.filter_by(activity_id=a.id, enrollment_id=enr.id).first()
        if not r:
            r = ActivityCompletion(activity_id=a.id, enrollment_id=enr.id)
            db.session.add(r)
        r.viewed_at = datetime.utcnow()
        r.completed_at = r.completed_at or datetime.utcnow()

    A["item_pct"] = item_pct

    # ── Activity settings (called by elearning.el_activity) ──────────────────────────────────────
    def save_opts(a, f):
        if a.kind == "quiz":
            ids = [int(x) for x in f.getlist("qids") if x.isdigit()]
            a.set_opts({"mode": "random" if f.get("mode") == "random" else "pick", "ids": ids,
                        "random_n": A["to_int"](f.get("random_n"), 0, 200), "category": (f.get("category") or "").strip(),
                        "time_limit": A["to_int"](f.get("time_limit"), 0, 600), "attempts": A["to_int"](f.get("attempts"), 0, 50),
                        "pass_pct": A["to_int"](f.get("pass_pct"), 0, 100), "shuffle": bool(f.get("shuffle")),
                        "review": f.get("review") if f.get("review") in ("after", "score", "never") else "after",
                        "grade_method": "last" if f.get("grade_method") == "last" else "best"})
            a.completion = "none" if a.completion == "none" else "auto"
        elif a.kind == "assign":
            a.set_opts({"due": (f.get("due") or "").strip(), "max_points": A["to_int"](f.get("max_points"), 1, 1000) or 20,
                        "allow_late": bool(f.get("allow_late")), "mode": f.get("mode") if f.get("mode") in ("text", "file", "both") else "both"})
            a.completion = "none" if a.completion == "none" else "auto"

    A["save_activity_opts"] = save_opts
    A["bank_for"] = lambda c: Question.query.filter_by(course_id=c.id).order_by(Question.category, Question.id).all()

    # ── Question bank ─────────────────────────────────────────────────────────────────────────────
    @app.route("/elearning/<int:cid>/questions", methods=["GET", "POST"])
    @staff
    def el_bank(cid):
        c = get_course(cid)
        if request.method == "POST":
            f = request.form
            if f.get("action") == "import":
                txt = f.get("gift") or ""
                up = request.files.get("file")
                if up and up.filename:
                    txt = up.read().decode("utf-8", "ignore")
                items = parse_gift(txt)
                for cat, qt, qtext, data, fb in items:
                    db.session.add(Question(course_id=c.id, category=(f.get("category") or cat or "").strip()[:80] or None,
                                            qtype=qt, text=qtext[:4000], data=json.dumps(data, ensure_ascii=False), feedback=fb))
                log("questions_import", f"{c.title} ({len(items)})")
                db.session.commit()
                flash(_("{n} question(s) importée(s).", n=len(items)) if items else
                      _("Aucune question reconnue. Vérifiez le format GIFT."), "success" if items else "error")
            elif f.get("action") == "delete":
                q = Question.query.get_or_404(f.get("qid", type=int))
                if q.course_id == c.id:
                    db.session.delete(q)
                    db.session.commit()
            return redirect(url_for("el_bank", cid=c.id))
        qs = A["bank_for"](c)
        cats = sorted({q.category or "" for q in qs})
        return render_template("el_bank.html", c=c, qs=qs, cats=cats, qtypes=QTYPES)

    @app.route("/elearning/<int:cid>/questions/edit", methods=["GET", "POST"])
    @app.route("/elearning/<int:cid>/questions/edit/<int:qid>", methods=["GET", "POST"])
    @staff
    def el_question(cid, qid=None):
        c = get_course(cid)
        q = Question.query.get_or_404(qid) if qid else None
        if q and q.course_id != c.id:
            abort(404)
        qt = q.qtype if q else (request.args.get("type") if request.args.get("type") in QTYPES else "mcq")
        if request.method == "POST":
            f = request.form
            text = (f.get("text") or "").strip()
            if not text:
                flash(_("L'énoncé est obligatoire."), "error")
                return redirect(request.url)
            if qt in ("mcq", "multi"):
                opts = []
                for i in range(8):
                    t = (f.get(f"o{i}") or "").strip()
                    if t:
                        ok = (f.get("ok") == str(i)) if qt == "mcq" else bool(f.get(f"ok{i}"))
                        opts.append({"t": t, "ok": ok, "fb": (f.get(f"fb{i}") or "").strip()})
                data = {"options": opts}
                if len(opts) < 2 or not any(o["ok"] for o in opts):
                    flash(_("Au moins deux propositions, dont une correcte."), "error")
                    return redirect(request.url)
            elif qt == "tf":
                data = {"answer": f.get("answer") == "1"}
            elif qt == "short":
                data = {"answers": [x.strip() for x in (f.get("answers") or "").split("\n") if x.strip()], "case": bool(f.get("case"))}
            elif qt == "numeric":
                try:
                    data = {"answer": float((f.get("answer") or "0").replace(",", ".")), "tol": float((f.get("tol") or "0").replace(",", "."))}
                except ValueError:
                    flash(_("Valeur numérique invalide."), "error")
                    return redirect(request.url)
            else:
                pairs = [[(f.get(f"l{i}") or "").strip(), (f.get(f"r{i}") or "").strip()] for i in range(8)]
                data = {"pairs": [p for p in pairs if p[0] and p[1]]}
                if len(data["pairs"]) < 2:
                    flash(_("Au moins deux paires."), "error")
                    return redirect(request.url)
            if not q:
                q = Question(course_id=c.id, qtype=qt)
                db.session.add(q)
            q.text, q.data = text[:4000], json.dumps(data, ensure_ascii=False)
            q.category = (f.get("category") or "").strip()[:80] or None
            q.points = max(0.25, min(100.0, float((f.get("points") or "1").replace(",", ".") or 1)))
            q.feedback = (f.get("feedback") or "").strip() or None
            db.session.commit()
            flash(_("Question enregistrée."), "success")
            return redirect(url_for("el_bank", cid=c.id) if not f.get("again") else url_for("el_question", cid=c.id, type=qt))
        cats = sorted({x.category for x in Question.query.filter_by(course_id=c.id) if x.category})
        return render_template("el_question.html", c=c, q=q, qt=qt, d=q.d() if q else {}, cats=cats)

    # ── Quiz results & item analysis (staff) ──────────────────────────────────────────────────────
    @app.route("/elearning/<int:cid>/quiz/<int:aid>")
    @staff
    def el_quiz_results(cid, aid):
        c = get_course(cid)
        a = Activity.query.get_or_404(aid)
        if a.course_id != c.id or a.kind != "quiz":
            abort(404)
        enrs = {e.id: e for e in c.formation.enrollments.filter_by(status="confirmed")}
        ats = QuizAttempt.query.filter(QuizAttempt.activity_id == a.id, QuizAttempt.finished_at.isnot(None)) \
            .order_by(QuizAttempt.finished_at.desc()).all()
        stats = {}
        for at in ats:
            lay, ans = json.loads(at.layout or "{}"), json.loads(at.answers or "{}")
            for qid in lay.get("q", []):
                q = db.session.get(Question, qid)
                if not q:
                    continue
                _e, frac = grade_question(q, ans.get(str(qid)))
                s = stats.setdefault(qid, {"q": q, "n": 0, "sum": 0})
                s["n"] += 1
                s["sum"] += frac
        items = sorted(stats.values(), key=lambda s: s["sum"] / s["n"])
        o = a.opts()
        best = {}
        for eid in enrs:
            r = quiz_result(a, eid)
            if r is not None:
                best[eid] = r
        return render_template("el_quiz_results.html", c=c, a=a, ats=ats, enrs=enrs, items=items, o=o, best=best)

    @app.route("/elearning/<int:cid>/quiz/<int:aid>/reset/<int:atid>", methods=["POST"])
    @staff
    def el_quiz_reset(cid, aid, atid):
        get_course(cid)
        at = QuizAttempt.query.get_or_404(atid)
        if at.activity_id != aid:
            abort(404)
        db.session.delete(at)
        db.session.commit()
        flash(_("Tentative supprimée : le stagiaire peut repasser le quiz."), "success")
        return redirect(url_for("el_quiz_results", cid=cid, aid=aid))

    # ── Assignment grading ────────────────────────────────────────────────────────────────────────
    @app.route("/elearning/<int:cid>/devoir/<int:aid>", methods=["GET", "POST"])
    @staff
    def el_assign(cid, aid):
        c = get_course(cid)
        a = Activity.query.get_or_404(aid)
        if a.course_id != c.id or a.kind != "assign":
            abort(404)
        o = a.opts()
        mx = float(o.get("max_points") or 20)
        if request.method == "POST":
            f = request.form
            s = Submission.query.get_or_404(f.get("sid", type=int))
            if s.activity_id != a.id:
                abort(404)
            g = (f.get("grade") or "").replace(",", ".").strip()
            try:
                s.grade = None if g == "" else max(0.0, min(mx, float(g)))
            except ValueError:
                flash(_("Note invalide."), "error")
                return redirect(request.url)
            s.feedback = (f.get("feedback") or "").strip() or None
            s.graded_at, s.graded_by = datetime.utcnow(), current_user.name
            if s.grade is not None and A.get("notify_graded"):
                A["notify_graded"](s, a)
            db.session.commit()
            flash(_("Note enregistrée."), "success")
            return redirect(url_for("el_assign", cid=c.id, aid=a.id) + f"#sub{s.id}")
        enrs = sorted(c.formation.enrollments.filter_by(status="confirmed").all(),
                      key=lambda e: (e.student.last_name.lower(), e.student.first_name.lower()))
        subs = {s.enrollment_id: s for s in Submission.query.filter_by(activity_id=a.id)}
        return render_template("el_assign.html", c=c, a=a, o=o, mx=mx, enrs=enrs, subs=subs)

    # ── Gradebook ─────────────────────────────────────────────────────────────────────────────────
    def gradebook(c):
        items = [x for x in c.activities(True) if x.kind in ("quiz", "assign")]
        enrs = sorted(c.formation.enrollments.filter_by(status="confirmed").all(),
                      key=lambda e: (e.student.last_name.lower(), e.student.first_name.lower()))
        rows = []
        for e in enrs:
            vals = [item_pct(x, e.id) for x in items]
            got = [v for v in vals if v is not None]
            rows.append({"enr": e, "vals": vals, "avg": round(sum(got) / len(got), 1) if got else None})
        return items, rows

    A["gradebook"] = gradebook

    @app.route("/elearning/<int:cid>/notes")
    @staff
    def el_gradebook(cid):
        c = get_course(cid)
        items, rows = gradebook(c)
        if request.args.get("format") == "xlsx":
            hdr = ["Stagiaire", "Entreprise"] + [f"{x.title} (/20)" for x in items] + ["Moyenne (/20)"]
            data = [[r["enr"].student.full_name, A["db"].session.get(A["Company"], r["enr"].student.company_id).name
                     if r["enr"].student.company_id else ""] +
                    [round(v / 5, 2) if v is not None else "" for v in r["vals"]] +
                    [round(r["avg"] / 5, 2) if r["avg"] is not None else ""] for r in rows]
            return A["xlsx"]([hdr] + data, f"notes_{c.id}.xlsx", [28, 24] + [16] * len(items) + [14])
        return render_template("el_gradebook.html", c=c, items=items, rows=rows)

    # ── Learner side ──────────────────────────────────────────────────────────────────────────────
    def learner_ctx(token, aid):
        enr = Enrollment.query.filter_by(token=token).first_or_404()
        a = Activity.query.get_or_404(aid)
        c = db.session.get(Course, a.course_id)
        prev = current_user.is_authenticated and current_user.role != "rh" and request.args.get("apercu") == "1"
        if enr.status != "confirmed" or c.formation_id != enr.formation_id or ((c.status != "published" or not a.visible) and not prev):
            abort(404)
        return enr, a, c, prev

    def draw_questions(a):
        o = a.opts()
        bank = Question.query.filter_by(course_id=a.course_id).all()
        if o.get("mode") == "random":
            pool = [q for q in bank if not o.get("category") or (q.category or "") == o.get("category")]
            random.shuffle(pool)
            qs = pool[: o.get("random_n") or len(pool)]
        else:
            byid = {q.id: q for q in bank}
            qs = [byid[i] for i in o.get("ids", []) if i in byid]
            if o.get("shuffle"):
                random.shuffle(qs)
        lay = {"q": [q.id for q in qs], "o": {}}
        for q in qs:
            n = len(q.d().get("options", [])) or len(q.d().get("pairs", []))
            order = list(range(n))
            if o.get("shuffle") or q.qtype == "match":
                random.shuffle(order)
            lay["o"][str(q.id)] = order
        return lay

    @app.route("/t/<token>/eval/<int:aid>", methods=["GET", "POST"])
    def learn_eval(token, aid):
        enr, a, c, prev = learner_ctx(token, aid)
        o = a.opts()
        q = "?apercu=1" if prev else ""
        if not prev and A["el_locked"](c, enr, a):
            flash(_("Cette ressource n'est pas encore accessible."), "error")
            return redirect(url_for("learn_course", token=token, cid=c.id))
        if a.kind == "quiz":
            ats = QuizAttempt.query.filter_by(activity_id=a.id, enrollment_id=enr.id).order_by(QuizAttempt.number).all()
            open_at = next((x for x in ats if not x.finished_at), None)
            done = [x for x in ats if x.finished_at]
            left = (o.get("attempts") or 0) - len(done) if o.get("attempts") else None
            if request.method == "POST" and not prev:
                if open_at:
                    return redirect(url_for("learn_attempt", token=token, atid=open_at.id))
                if left is not None and left <= 0:
                    flash(_("Vous avez utilisé toutes vos tentatives."), "error")
                    return redirect(request.url)
                lay = draw_questions(a)
                if not lay["q"]:
                    flash(_("Ce quiz ne contient pas encore de questions."), "error")
                    return redirect(request.url)
                at = QuizAttempt(activity_id=a.id, enrollment_id=enr.id, number=len(ats) + 1, layout=json.dumps(lay))
                if o.get("time_limit"):
                    at.deadline = datetime.utcnow() + timedelta(minutes=o["time_limit"])
                db.session.add(at)
                db.session.commit()
                return redirect(url_for("learn_attempt", token=token, atid=at.id))
            nq = o.get("random_n") if o.get("mode") == "random" else len(o.get("ids", []))
            return render_template("learn_quiz_intro.html", enr=enr, a=a, c=c, o=o, ats=done, open_at=open_at, left=left,
                                   best=quiz_result(a, enr.id), nq=nq, preview=prev, q=q)
        if a.kind == "assign":
            s = Submission.query.filter_by(activity_id=a.id, enrollment_id=enr.id).first()
            due = None
            try:
                due = datetime.strptime(o.get("due") or "", "%Y-%m-%dT%H:%M")
            except ValueError:
                pass
            now = A["local_now"]().replace(tzinfo=None)
            closed = bool(due and now > due and not o.get("allow_late"))
            if request.method == "POST" and not prev:
                if closed:
                    flash(_("La date limite est dépassée."), "error")
                    return redirect(request.url)
                if s and s.grade is not None:
                    flash(_("Ce devoir est déjà noté."), "error")
                    return redirect(request.url)
                text = (request.form.get("text") or "").strip()
                fs = request.files.get("file")
                if not text and not (fs and fs.filename) and not (s and s.file_id):
                    flash(_("Ajoutez un texte ou un fichier."), "error")
                    return redirect(request.url)
                if not s:
                    s = Submission(activity_id=a.id, enrollment_id=enr.id)
                    db.session.add(s)
                s.text = text or None
                if fs and fs.filename:
                    data = fs.read()
                    if len(data) > 20 * 1024 * 1024:
                        flash(_("Fichier trop volumineux (20 Mo maximum)."), "error")
                        return redirect(request.url)
                    sf = StoredFile(name=fs.filename[-200:], mime=fs.mimetype, size=len(data), data=data)
                    db.session.add(sf)
                    db.session.flush()
                    s.file_id = sf.id
                s.submitted_at = datetime.utcnow()
                s.late = bool(due and now > due)
                complete(enr, a)
                db.session.commit()
                flash(_("Devoir remis. Votre formateur va le corriger."), "success")
                return redirect(request.url)
            return render_template("learn_assign.html", enr=enr, a=a, c=c, o=o, s=s, due=due, closed=closed, preview=prev, q=q,
                                   mx=float(o.get("max_points") or 20))
        abort(404)

    @app.route("/t/<token>/tentative/<int:atid>", methods=["GET", "POST"])
    def learn_attempt(token, atid):
        enr = Enrollment.query.filter_by(token=token).first_or_404()
        at = QuizAttempt.query.get_or_404(atid)
        if at.enrollment_id != enr.id:
            abort(404)
        a = db.session.get(Activity, at.activity_id)
        c = db.session.get(Course, a.course_id)
        o, lay = a.opts(), json.loads(at.layout or "{}")
        qs = [db.session.get(Question, i) for i in lay.get("q", [])]
        qs = [x for x in qs if x]
        if at.finished_at:
            return render_template("learn_quiz_review.html", enr=enr, a=a, c=c, o=o, at=at, lay=lay, qs=qs,
                                   ans=json.loads(at.answers or "{}"), grade=grade_question, correct_text=correct_text)
        expired = at.deadline and datetime.utcnow() > at.deadline + timedelta(seconds=30)
        if request.method == "POST" or expired:
            ans = {}
            f = request.form if not expired else {}       # after the deadline (+30 s grace) answers are ignored
            if expired:
                from werkzeug.datastructures import MultiDict
                f = MultiDict()
            for x in qs:
                k = str(x.id)
                if x.qtype == "multi":
                    ans[k] = f.getlist("q" + k)
                elif x.qtype == "match":
                    ans[k] = {str(i): f.get(f"q{k}_{i}", "") for i in range(len(x.d().get("pairs", [])))}
                else:
                    ans[k] = f.get("q" + k, "")
            score = mx = 0.0
            for x in qs:
                e, _fr = grade_question(x, ans.get(str(x.id)))
                score += e
                mx += x.points or 1
            at.answers = json.dumps(ans, ensure_ascii=False)
            at.score, at.max_score = round(score, 2), round(mx, 2)
            at.pct = round(score * 100 / mx, 1) if mx else 0
            at.finished_at = datetime.utcnow()
            if a.completion != "none" and at.pct >= (o.get("pass_pct") or 0):
                complete(enr, a)
            db.session.commit()
            return redirect(url_for("learn_attempt", token=token, atid=at.id))
        remaining = int((at.deadline - datetime.utcnow()).total_seconds()) if at.deadline else None
        return render_template("learn_quiz_take.html", enr=enr, a=a, c=c, o=o, at=at, lay=lay, qs=qs, remaining=remaining)

    @app.route("/t/<token>/notes/<int:cid>")
    def learn_grades(token, cid):
        enr = Enrollment.query.filter_by(token=token).first_or_404()
        c = Course.query.get_or_404(cid)
        if c.formation_id != enr.formation_id or c.status != "published":
            abort(404)
        items = [x for x in c.activities(True) if x.kind in ("quiz", "assign")]
        vals = [(x, item_pct(x, enr.id)) for x in items]
        got = [v for _x, v in vals if v is not None]
        fbs = {s.activity_id: s for s in Submission.query.filter_by(enrollment_id=enr.id)}
        return render_template("learn_grades.html", enr=enr, c=c, vals=vals, avg=round(sum(got) / len(got), 1) if got else None, fbs=fbs)

    A["_PUBLIC"].update({ep: (Enrollment, "token", "token") for ep in ("learn_eval", "learn_attempt", "learn_grades")})
