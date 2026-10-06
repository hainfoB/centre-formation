"""Centre de formation — gestion des formations, inscriptions, séances, présences (QR), paiements,
relances, attestations et espace parents. Flask + SQLAlchemy, prêt pour Railway."""
import calendar
import hashlib
import hmac
import io
import os
import re
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file,
                   session, url_for)
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func, or_

from models import (FORMATION_STATUSES, PAY_METHODS, ROLES, Attendance, AuditLog, Enrollment, Formation, Payment,
                    Seance, Setting, Student, User, db)
from notify import html_mail, send_email, wa_link

# ── App setup ─────────────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "dev-" + hashlib.sha256(os.urandom(16)).hexdigest()
_db_url = os.environ.get("DATABASE_URL", "sqlite:///centre.db")
if _db_url.startswith("postgres://"):
    _db_url = "postgresql://" + _db_url[len("postgres://"):]
if _db_url.startswith("postgresql://"):          # SQLAlchemy 2.1 defaults to psycopg 3: use the installed psycopg2
    _db_url = "postgresql+psycopg2://" + _db_url[len("postgresql://"):]
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["WTF_CSRF_TIME_LIMIT"] = None
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
APP_URL = os.environ.get("APP_URL", "").rstrip("/")
UTC_OFFSET = int(os.environ.get("UTC_OFFSET_HOURS", "1"))   # Algérie = UTC+1

db.init_app(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Veuillez vous connecter."


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


with app.app_context():
    db.create_all()


def base_url():
    return APP_URL or request.host_url.rstrip("/")


# ── Small helpers ─────────────────────────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def local_now():
    return datetime.utcnow() + timedelta(hours=UTC_OFFSET)


def today():
    return local_now().date()


def parse_date(v):
    try:
        return datetime.strptime((v or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def hm_to_min(v):
    m = _HM_RE.match((v or "").strip())
    return int(m[1]) * 60 + int(m[2]) if m else None


def to_int(v, lo=0, hi=10_000_000):
    try:
        n = int(float(str(v or "0").replace(" ", "").replace(",", ".")))
    except ValueError:
        n = 0
    return min(max(n, lo), hi)


def slugify(text, exclude_id=None):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower().encode("ascii", "ignore").decode()).strip("-")[:60] or "formation"
    slug, i = base, 2
    while True:
        q = Formation.query.filter_by(slug=slug)
        if exclude_id:
            q = q.filter(Formation.id != exclude_id)
        if not q.first():
            return slug
        slug, i = f"{base}-{i}", i + 1


def add_months(d, k):
    y, m = divmod(d.month - 1 + k, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def fmt_da(n):
    return f"{int(n or 0):,}".replace(",", " ") + " DA"


def log(action, details=""):
    who = current_user.name if current_user and current_user.is_authenticated else "public"
    db.session.add(AuditLog(user=who, action=action, details=(details or "")[:300]))


def role_required(what):
    def deco(fn):
        @wraps(fn)
        @login_required
        def inner(*a, **kw):
            if not current_user.can(what):
                flash("Accès réservé.", "error")
                return redirect(url_for("dashboard"))
            return fn(*a, **kw)
        return inner
    return deco


LABELS = {
    "draft": "Brouillon", "open": "Inscriptions ouvertes", "running": "En cours", "done": "Terminée",
    "pending": "En attente", "confirmed": "Confirmé", "waitlist": "Liste d'attente", "cancelled": "Annulé",
    "present": "Présent", "late": "Retard", "absent": "Absent", "excused": "Excusé",
    "due": "À payer", "paid": "Payé", "waived": "Exonéré", "overdue": "En retard",
    "cash": "Espèces", "ccp": "CCP", "virement": "Virement", "cheque": "Chèque", "autre": "Autre",
    "onsite": "Présentiel", "online": "En ligne", "hybrid": "Hybride",
    "admin": "Administrateur", "secretaire": "Secrétariat", "formateur": "Formateur",
    "ticket": "QR ticket", "room": "QR salle", "code": "Code", "manual": "Manuel",
}


@app.context_processor
def inject():
    return {"L": LABELS, "centre": Setting.all(), "today": today(), "fmt_da": fmt_da,
            "roles": ROLES, "pay_methods": PAY_METHODS, "statuses": FORMATION_STATUSES}


app.jinja_env.filters["da"] = fmt_da
app.jinja_env.filters["d"] = lambda d: d.strftime("%d/%m/%Y") if d else ""
app.jinja_env.filters["hm"] = lambda dt: (dt + timedelta(hours=UTC_OFFSET)).strftime("%H:%M") if dt else ""


def qr_svg(text, scale=6):
    import segno
    return segno.make(text, error="m").svg_inline(scale=scale, dark="#0b2545", light="#ffffff", border=2)


# ── First run / auth ──────────────────────────────────────────────────────────────────────────────
@app.before_request
def first_run():
    if request.endpoint in ("setup", "static") or request.path.startswith("/static"):
        return None
    if not User.query.first():
        return redirect(url_for("setup"))
    return None


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.first():
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        email, pw = f.get("email", "").strip().lower(), f.get("password", "")
        if not _EMAIL_RE.match(email) or len(pw) < 8 or not f.get("name", "").strip():
            flash("Nom, email valide et mot de passe (8 caractères min.) requis.", "error")
        else:
            u = User(name=f["name"].strip()[:120], email=email, role="admin")
            u.set_password(pw)
            db.session.add(u)
            if f.get("centre_name", "").strip():
                Setting.set("centre_name", f["centre_name"].strip()[:120])
            db.session.commit()
            login_user(u, remember=True)
            flash("Bienvenue ! Complétez les informations du centre.", "success")
            return redirect(url_for("settings"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        u = User.query.filter_by(email=request.form.get("email", "").strip().lower()).first()
        if u and u.active and u.check_password(request.form.get("password", "")):
            login_user(u, remember=True)
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Identifiants incorrects.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


# ── Enrollment / payment / attendance logic ───────────────────────────────────────────────────────
def find_or_create_student(f):
    """Reuse a student with the same email or phone, otherwise create one; updates given fields."""
    email = (f.get("email") or "").strip().lower() or None
    phone = (f.get("phone") or "").strip()[:30] or None
    st = None
    if email:
        st = Student.query.filter(func.lower(Student.email) == email).first()
    if not st and phone:
        norm = Student.norm_phone(phone)
        st = next((s for s in Student.query.filter(Student.phone.isnot(None)).all()
                   if Student.norm_phone(s.phone) == norm), None)
    if not st:
        st = Student(first_name="", last_name="")
        db.session.add(st)
    apply_student_form(st, f)
    return st


def apply_student_form(st, f):
    for field, n in (("first_name", 80), ("last_name", 80), ("phone", 30), ("birth_place", 100), ("address", 200),
                     ("level", 100), ("parent_name", 120), ("parent_phone", 30)):
        if f.get(field) is None:
            continue
        v = f.get(field).strip()[:n]
        if field in ("first_name", "last_name"):
            if v:
                setattr(st, field, v)
        else:
            setattr(st, field, v or None)
    for field in ("email", "parent_email"):
        if f.get(field) is not None:
            v = (f.get(field) or "").strip().lower()[:150]
            setattr(st, field, v if _EMAIL_RE.match(v) else None)
    if f.get("birth_date") is not None:
        st.birth_date = parse_date(f.get("birth_date"))
    if f.get("notes") is not None:
        st.notes = (f.get("notes") or "").strip()[:2000] or None


def ensure_schedule(enr, force=False):
    """Registration fee (n°0) + tuition split into monthly instalments. Idempotent unless force."""
    fo = enr.formation
    if fo.is_free or enr.status != "confirmed":
        return False
    if enr.payments.count():
        if not force:
            return False
        for p in enr.payments.all():
            db.session.delete(p)
        db.session.flush()
    first = fo.seances.first()
    start = max((first.day if first else fo.starts_on) or today(), today())
    if fo.registration_fee:
        db.session.add(Payment(enrollment_id=enr.id, number=0, label="Frais d'inscription", due_on=today(),
                               amount=fo.registration_fee))
    tuition = max(0, (fo.price_total or 0) - (enr.discount or 0))
    n = max(1, min(fo.installments or 1, 36))
    if tuition:
        base, rest = divmod(tuition, n)
        for k in range(n):
            db.session.add(Payment(enrollment_id=enr.id, number=k + 1,
                                   label="Paiement unique" if n == 1 else f"Mensualité {k + 1}/{n}",
                                   due_on=add_months(start, k), amount=base + (rest if k == 0 else 0)))
    db.session.commit()
    return True


def payment_state(p, d=None):
    if p.status in ("paid", "waived"):
        return p.status
    return "overdue" if p.due_on < (d or today()) else "due"


def enrollment_money(enr):
    pays = sorted(enr.payments.all(), key=lambda p: (p.due_on, p.number))
    exp = sum(p.amount for p in pays if p.status != "waived")
    paid = sum(p.amount for p in pays if p.status == "paid")
    over = sum(p.amount for p in pays if payment_state(p) == "overdue")
    return {"pays": pays, "expected": exp, "paid": paid, "remaining": exp - paid, "overdue": over}


def send_enrollment_mail(enr):
    st, fo, c = enr.student, enr.formation, Setting.get("centre_name")
    to = st.email or st.parent_email
    if not to:
        return
    ticket = f"{base_url()}/t/{enr.token}"
    if enr.status == "confirmed":
        title = "Inscription confirmée"
        paras = [f"Bonjour {st.first_name},", f"Votre inscription à la formation <b>{fo.title}</b> est confirmée.",
                 "Votre espace personnel contient votre QR code de présence, le planning des séances et vos paiements."]
    elif enr.status == "waitlist":
        title = "Liste d'attente"
        paras = [f"Bonjour {st.first_name},", f"La formation <b>{fo.title}</b> est complète : vous êtes sur liste "
                 "d'attente. Nous vous prévenons dès qu'une place se libère."]
    else:
        title = "Demande d'inscription reçue"
        paras = [f"Bonjour {st.first_name},", f"Nous avons bien reçu votre demande pour <b>{fo.title}</b>. "
                 "Le secrétariat vous contactera pour la valider."]
    text = "\n".join(re.sub("<[^>]+>", "", p) for p in paras) + f"\n{ticket}"
    send_email(to, f"{title} — {fo.title}", text,
               html_mail(c, title, paras, ("Mon espace", ticket) if enr.status == "confirmed" else None), c)


def confirm_enrollment(enr):
    was = enr.status
    enr.status = "confirmed"
    db.session.commit()
    ensure_schedule(enr)
    if was != "confirmed":
        send_enrollment_mail(enr)


def promote_waitlist(fo):
    while True:
        left = fo.seats_left()
        if left is not None and left <= 0:
            break
        nxt = fo.enrollments.filter_by(status="waitlist").order_by(Enrollment.created_at).first()
        if not nxt:
            break
        confirm_enrollment(nxt)


def attendance_stats(fo):
    """Rate per confirmed enrollment over séances already held (date reached or marked)."""
    seances = fo.seances.all()
    ids = [s.id for s in seances] or [0]
    marked = {a.seance_id for a in Attendance.query.filter(Attendance.seance_id.in_(ids), Attendance.status.isnot(None))}
    held = [s for s in seances if s.day <= today() or s.id in marked]
    held_ids = {s.id for s in held} or {0}
    by_enr = {}
    for a in Attendance.query.filter(Attendance.seance_id.in_(held_ids)):
        by_enr.setdefault(a.enrollment_id, {})[a.seance_id] = a
    rows = []
    q = (fo.enrollments.filter_by(status="confirmed").join(Student)
         .order_by(Student.last_name, Student.first_name))
    for e in q:
        marks = by_enr.get(e.id, {})
        attended = sum(1 for a in marks.values() if a.status in ("present", "late"))
        late = sum(1 for a in marks.values() if a.status == "late")
        excused = sum(1 for a in marks.values() if a.status == "excused")
        denom = len(held) - excused
        rate = round(attended * 100 / denom) if denom > 0 else 0
        minutes = sum(s.duration_minutes() for s in held if s.id in marks and marks[s.id].status in ("present", "late"))
        rows.append({"enr": e, "attended": attended, "late": late, "excused": excused,
                     "absent": max(0, len(held) - attended - excused), "rate": rate, "marks": marks,
                     "hours": round(minutes / 60, 1), "eligible_att": bool(held) and rate >= (fo.min_attendance or 0)})
    avg = round(sum(r["rate"] for r in rows) / len(rows)) if rows else 0
    return {"held": held, "rows": rows, "avg": avg, "seances": seances}


def enrollment_stats(enr):
    st = attendance_stats(enr.formation)
    return next((r for r in st["rows"] if r["enr"].id == enr.id), None) or \
        {"rate": 0, "attended": 0, "late": 0, "absent": 0, "excused": 0, "hours": 0, "eligible_att": False, "marks": {}}


def cert_eligible(enr, row=None):
    """(ok, reason) — attendance threshold reached, and fully paid when the formation requires it."""
    if enr.status != "confirmed":
        return False, "Inscription non confirmée"
    row = row or enrollment_stats(enr)
    if not row["eligible_att"]:
        return False, f"Assiduité insuffisante ({row['rate']} % < {enr.formation.min_attendance} %)"
    if enr.formation.cert_requires_payment and enrollment_money(enr)["remaining"] > 0:
        return False, "Reste à payer"
    return True, ""


def set_attendance(seance, enr, status):
    att = Attendance.query.filter_by(seance_id=seance.id, enrollment_id=enr.id).first()
    if not att:
        att = Attendance(seance_id=seance.id, enrollment_id=enr.id)
        db.session.add(att)
    att.status = status or None
    return att


def ensure_tokens(s):
    changed = False
    for field in ("scan_token", "room_token"):
        if not getattr(s, field):
            setattr(s, field, secrets.token_urlsafe(12))
            changed = True
    if not s.code_in:
        gen_codes(s)
        changed = True
    if changed:
        db.session.commit()


def gen_codes(s):
    a = f"{secrets.randbelow(10000):04d}"
    b = a
    while b == a:
        b = f"{secrets.randbelow(10000):04d}"
    s.code_in, s.code_out = a, b


SCAN_MSG = {
    "in_ok": "Entrée enregistrée à {t}", "in_late": "Entrée enregistrée à {t} (retard)",
    "in_dup": "Déjà enregistré(e) à {t}", "out_ok": "Sortie enregistrée à {t}", "out_dup": "Sortie déjà enregistrée à {t}",
    "no_entry": "Aucune entrée enregistrée", "no_exit": "Pas de pointage de sortie pour cette formation",
    "wrong_day": "Cette séance n'a pas lieu aujourd'hui", "not_enrolled": "Inscription non confirmée",
    "unknown": "QR code inconnu", "unknown_email": "Email ou téléphone non reconnu", "bad_code": "Code incorrect",
}


def do_checkin(s, enr, direction, method):
    fo = s.formation
    if enr.formation_id != fo.id or enr.status != "confirmed":
        return False, "not_enrolled", ""
    if s.day != today():
        return False, "wrong_day", ""
    att = Attendance.query.filter_by(seance_id=s.id, enrollment_id=enr.id).first()
    now = datetime.utcnow()
    hm = (now + timedelta(hours=UTC_OFFSET)).strftime("%H:%M")
    if direction == "out":
        if fo.check_mode != "inout":
            return False, "no_exit", ""
        if not att or not att.checkin_at:
            return False, "no_entry", ""
        if att.checkout_at:
            return True, "out_dup", (att.checkout_at + timedelta(hours=UTC_OFFSET)).strftime("%H:%M")
        att.checkout_at = now
        db.session.commit()
        return True, "out_ok", hm
    if att and att.checkin_at:
        return True, "in_dup", (att.checkin_at + timedelta(hours=UTC_OFFSET)).strftime("%H:%M")
    b = s.bounds()
    loc = now + timedelta(hours=UTC_OFFSET)
    late = b is not None and loc.hour * 60 + loc.minute > b[0] + (fo.late_tolerance or 0)
    att = set_attendance(s, enr, "late" if late else "present")
    att.checkin_at, att.method = now, method
    db.session.commit()
    return True, ("in_late" if late else "in_ok"), hm


def scan_msg(key, t=""):
    return SCAN_MSG.get(key, key).format(t=t)


def find_by_contact(fo, raw):
    """Confirmed enrollment of a formation by student email or phone."""
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    q = fo.enrollments.join(Student)
    if "@" in raw:
        return q.filter(func.lower(Student.email) == raw).first()
    norm = Student.norm_phone(raw)
    return next((e for e in q.all() if e.student.phone and Student.norm_phone(e.student.phone) == norm), None) \
        if len(norm) >= 8 else None


ROOM_WINDOW = 45


def room_sig(sid, direction, window):
    return hmac.new(app.config["SECRET_KEY"].encode(), f"{sid}|{direction}|{window}".encode(),
                    hashlib.sha256).hexdigest()[:16]


def room_window():
    return int(datetime.utcnow().timestamp() // ROOM_WINDOW)


# ── Dashboard ─────────────────────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def dashboard():
    if current_user.role == "formateur":
        return redirect(url_for("my_seances"))
    d0 = today().replace(day=1)
    collected_month = db.session.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.status == "paid", Payment.paid_on >= d0).scalar()
    overdue_q = (Payment.query.join(Enrollment).join(Formation)
                 .filter(Payment.status == "due", Payment.due_on < today(), Enrollment.status == "confirmed",
                         Formation.status != "draft").order_by(Payment.due_on))
    overdue = overdue_q.limit(15).all()
    overdue_total = sum(p.amount for p in overdue_q.all())
    kpi = {
        "students": db.session.query(func.count(func.distinct(Enrollment.student_id))).join(Formation)
        .filter(Enrollment.status == "confirmed", Formation.status.in_(("open", "running"))).scalar(),
        "formations": Formation.query.filter(Formation.status.in_(("open", "running"))).count(),
        "pending": Enrollment.query.filter_by(status="pending").count(),
        "collected_month": collected_month, "overdue_total": overdue_total,
    }
    seances_today = Seance.query.filter_by(day=today()).all()
    upcoming = Seance.query.filter(Seance.day > today(), Seance.day <= today() + timedelta(days=7)).order_by(Seance.day).all()
    pending = Enrollment.query.filter_by(status="pending").order_by(Enrollment.created_at.desc()).limit(10).all()
    return render_template("dashboard.html", kpi=kpi, overdue=overdue, seances_today=seances_today,
                           upcoming=upcoming, pending=pending, state=payment_state, wa=wa_link)


@app.route("/mes-seances")
@login_required
def my_seances():
    q = Seance.query.join(Formation).filter(Seance.day >= today() - timedelta(days=7))
    if current_user.role == "formateur":
        q = q.filter(Formation.trainer_id == current_user.id)
    return render_template("my_seances.html", seances=q.order_by(Seance.day).limit(60).all())


# ── Formations ────────────────────────────────────────────────────────────────────────────────────
def apply_formation_form(fo, f):
    fo.title = f.get("title", "").strip()[:160] or fo.title
    for field, n in (("description", 4000), ("programme", 6000), ("prerequisites", 2000), ("audience", 200),
                     ("trainer", 120), ("location", 200), ("payment_info", 1000)):
        setattr(fo, field, f.get(field, "").strip()[:n] or None)
    fo.is_free = bool(f.get("is_free"))
    fo.auto_confirm = bool(f.get("auto_confirm"))
    fo.cert_requires_payment = bool(f.get("cert_requires_payment"))
    fo.starts_on, fo.ends_on = parse_date(f.get("starts_on")), parse_date(f.get("ends_on"))
    for field, hi in (("price_total", 10_000_000), ("registration_fee", 1_000_000), ("installments", 36),
                      ("hours_total", 5000), ("seats", 5000), ("min_attendance", 100), ("late_tolerance", 120)):
        setattr(fo, field, to_int(f.get(field), 0, hi))
    for field, default in (("late_tolerance", 15), ("min_attendance", 80), ("installments", 1)):
        if not str(f.get(field) or "").strip():
            setattr(fo, field, default)
    if fo.is_free:
        fo.price_total, fo.registration_fee, fo.installments = 0, 0, 1
    fo.installments = max(1, fo.installments or 1)
    fo.mode = f.get("mode") if f.get("mode") in ("onsite", "online", "hybrid") else "onsite"
    for field in ("online_url", "group_url"):
        u = f.get(field, "").strip()[:300]
        setattr(fo, field, u if u.lower().startswith(("http://", "https://")) else None)
    for field in ("start_time", "end_time"):
        v = f.get(field, "").strip()
        setattr(fo, field, v if hm_to_min(v) is not None else None)
    fo.check_mode = "inout" if f.get("check_mode") == "inout" else "in"
    tid = to_int(f.get("trainer_id"), 0, 10 ** 9)
    fo.trainer_id = tid if tid and db.session.get(User, tid) else None
    if fo.trainer_id and not fo.trainer:
        fo.trainer = db.session.get(User, fo.trainer_id).name


@app.route("/formations", methods=["GET", "POST"])
@role_required("manage")
def formations():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Le titre est obligatoire.", "error")
            return redirect(url_for("formations"))
        fo = Formation(title=title[:160], slug=slugify(title))
        apply_formation_form(fo, request.form)
        db.session.add(fo)
        log("formation_created", fo.title)
        db.session.commit()
        flash("Formation créée. Ajoutez maintenant les séances.", "success")
        return redirect(url_for("formation_detail", fid=fo.id))
    items = Formation.query.order_by(Formation.created_at.desc()).all()
    counts = {fo.id: {k: fo.enrollments.filter_by(status=k).count() for k in ("confirmed", "pending", "waitlist")}
              for fo in items}
    trainers = User.query.filter_by(role="formateur", active=True).all()
    return render_template("formations.html", items=items, counts=counts, trainers=trainers, fo=None)


@app.route("/formations/<int:fid>")
@role_required("manage")
def formation_detail(fid):
    fo = Formation.query.get_or_404(fid)
    stats = attendance_stats(fo)
    by_enr = {r["enr"].id: r for r in stats["rows"]}
    money = {e.id: enrollment_money(e) for e in fo.enrollments.filter_by(status="confirmed")}
    trainers = User.query.filter_by(role="formateur", active=True).all()
    return render_template("formation_detail.html", fo=fo, enrollments=fo.enrollments.all(), stats=stats,
                           by_enr=by_enr, money=money, trainers=trainers,
                           public_url=f"{base_url()}/f/{fo.slug}", tab=request.args.get("tab", "inscrits"))


@app.route("/formations/<int:fid>/action", methods=["POST"])
@role_required("manage")
def formation_action(fid):
    fo = Formation.query.get_or_404(fid)
    f, action = request.form, request.form.get("action", "")
    tab = "inscrits"
    if action == "edit":
        apply_formation_form(fo, f)
        db.session.commit()
        flash("Formation enregistrée.", "success")
        tab = "infos"
    elif action == "status" and f.get("status") in FORMATION_STATUSES:
        fo.status = f["status"]
        log("formation_status", f"{fo.title} → {fo.status}")
        db.session.commit()
        flash("Statut mis à jour.", "success")
    elif action in ("add_seance", "gen_seances"):
        tab = "seances"
        d = parse_date(f.get("day"))
        a, b = hm_to_min(f.get("start_time")), hm_to_min(f.get("end_time"))
        label = f"{f['start_time'].strip()} - {f['end_time'].strip()}" if a is not None and b is not None and b > a else None
        if not d:
            flash("Date invalide.", "error")
        elif action == "add_seance":
            db.session.add(Seance(formation_id=fo.id, day=d, time_label=label,
                                  topic=f.get("topic", "").strip()[:200] or None,
                                  trainer=f.get("trainer", "").strip()[:120] or None))
            db.session.commit()
        else:
            n, every = to_int(f.get("count"), 1, 200), to_int(f.get("every_days"), 1, 31)
            days = [int(x) for x in f.getlist("weekdays") if x.isdigit()]
            created, cur = 0, d
            if days:      # given weekdays (0 = lundi … 6 = dimanche), n séances in total
                while created < n:
                    if cur.weekday() in days:
                        db.session.add(Seance(formation_id=fo.id, day=cur, time_label=label))
                        created += 1
                    cur += timedelta(days=1)
            else:
                for i in range(n):
                    db.session.add(Seance(formation_id=fo.id, day=d + timedelta(days=every * i), time_label=label))
                created = n
            db.session.commit()
            flash(f"{created} séances créées.", "success")
    elif action == "del_seance":
        tab = "seances"
        s = Seance.query.filter_by(id=f.get("seance_id", type=int), formation_id=fo.id).first()
        if s:
            db.session.delete(s)
            db.session.commit()
    elif action == "enr_add":
        sid = to_int(f.get("student_id"), 0, 10 ** 9)
        st = db.session.get(Student, sid) if sid else None
        if not st:
            if not f.get("last_name", "").strip() or not f.get("first_name", "").strip():
                flash("Nom et prénom requis.", "error")
                return redirect(url_for("formation_detail", fid=fo.id))
            st = find_or_create_student(f)
            db.session.flush()
        if fo.enrollments.filter_by(student_id=st.id).first():
            flash("Ce stagiaire est déjà inscrit.", "error")
        else:
            enr = Enrollment(formation_id=fo.id, student_id=st.id, source="secretariat",
                             discount=to_int(f.get("discount"), 0, 10_000_000))
            db.session.add(enr)
            db.session.commit()
            confirm_enrollment(enr)
            log("enrollment_added", f"{st.full_name} → {fo.title}")
            db.session.commit()
            flash(f"{st.full_name} inscrit(e).", "success")
    elif action in ("enr_confirm", "enr_cancel", "enr_delete", "enr_discount"):
        enr = Enrollment.query.filter_by(id=f.get("enrollment_id", type=int), formation_id=fo.id).first_or_404()
        if action == "enr_confirm":
            confirm_enrollment(enr)
        elif action == "enr_cancel":
            enr.status = "cancelled"
            for p in enr.payments.filter_by(status="due"):
                db.session.delete(p)
            db.session.commit()
            promote_waitlist(fo)
        elif action == "enr_delete":
            db.session.delete(enr)
            db.session.commit()
            promote_waitlist(fo)
        else:
            enr.discount = to_int(f.get("discount"), 0, 10_000_000)
            db.session.commit()
            if enr.payments.filter(Payment.status != "due").count():
                flash("Remise enregistrée. Des paiements existent déjà : échéancier non recalculé.", "error")
            else:
                ensure_schedule(enr, force=True)
                flash("Remise appliquée, échéancier recalculé.", "success")
            return redirect(url_for("payments_view", fid=fo.id) + f"#e{enr.id}")
        log(action, f"{enr.student.full_name} / {fo.title}")
        db.session.commit()
    return redirect(url_for("formation_detail", fid=fo.id, tab=tab))


# ── Séances & attendance ──────────────────────────────────────────────────────────────────────────
def can_access_seance(s):
    return current_user.role != "formateur" or s.formation.trainer_id == current_user.id


@app.route("/seances/<int:sid>", methods=["GET", "POST"])
@login_required
def seance_view(sid):
    s = Seance.query.get_or_404(sid)
    if not can_access_seance(s):
        abort(403)
    fo = s.formation
    enrs = (fo.enrollments.filter_by(status="confirmed").join(Student)
            .order_by(Student.last_name, Student.first_name).all())
    if request.method == "POST":
        if request.form.get("form_action") == "gen_codes":
            gen_codes(s)
            db.session.commit()
            flash("Nouveaux codes générés.", "success")
            return redirect(url_for("seance_view", sid=s.id))
        if request.form.get("form_action") == "all_present":
            for e in enrs:
                a = Attendance.query.filter_by(seance_id=s.id, enrollment_id=e.id).first()
                if not a or not a.status:
                    set_attendance(s, e, "present").method = "manual"
            db.session.commit()
            return redirect(url_for("seance_view", sid=s.id))
        if request.form.get("topic") is not None:
            s.topic = request.form.get("topic", "").strip()[:200] or None
        for e in enrs:
            stv = request.form.get(f"st_{e.id}")
            if stv is None:
                continue
            if stv in ("present", "absent", "late", "excused", ""):
                att = set_attendance(s, e, stv)
                if stv and not att.method:
                    att.method = "manual"
        log("attendance_saved", f"{fo.title} {s.day}")
        db.session.commit()
        flash("Présences enregistrées.", "success")
        return redirect(url_for("seance_view", sid=s.id))
    ensure_tokens(s)
    att = {a.enrollment_id: a for a in s.attendances}
    counts = {k: sum(1 for a in att.values() if a.status == k) for k in ("present", "late", "absent", "excused")}
    return render_template("seance.html", s=s, fo=fo, enrs=enrs, att=att, counts=counts,
                           scan_url=f"{base_url()}/scan/{s.scan_token}", room_url=f"{base_url()}/room/{s.room_token}",
                           presence_url=f"{base_url()}/f/{fo.slug}/presence")


@app.route("/scan/<token>")
def scan_page(token):
    s = Seance.query.filter_by(scan_token=token).first_or_404()
    return render_template("scan.html", s=s, fo=s.formation, token=token)


@app.route("/scan/<token>/check", methods=["POST"])
def scan_check(token):
    s = Seance.query.filter_by(scan_token=token).first_or_404()
    data = request.get_json(silent=True) or {}
    raw = (data.get("code") or "").strip()
    raw = raw[4:] if raw.upper().startswith("CF1:") else raw.rsplit("/", 1)[-1]
    enr = Enrollment.query.filter_by(formation_id=s.formation_id, token=raw).first() if raw else None
    if not enr:
        return jsonify(ok=False, name="", message=scan_msg("unknown"))
    ok, key, t = do_checkin(s, enr, "out" if data.get("dir") == "out" else "in", "ticket")
    return jsonify(ok=ok, name=enr.student.full_name, message=scan_msg(key, t))


@app.route("/room/<token>")
def room_display(token):
    s = Seance.query.filter_by(room_token=token).first_or_404()
    return render_template("room.html", s=s, fo=s.formation, token=token, window=ROOM_WINDOW)


@app.route("/room/<token>/qr.json")
def room_qr(token):
    s = Seance.query.filter_by(room_token=token).first_or_404()
    direction = "out" if request.args.get("dir") == "out" and s.formation.check_mode == "inout" else "in"
    w = room_window()
    url = f"{base_url()}/checkin/{s.id}/{direction}/{w}/{room_sig(s.id, direction, w)}"
    resp = jsonify(svg=qr_svg(url, 10))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/checkin/<int:sid>/<direction>/<int:window>/<sig>", methods=["GET", "POST"])
def room_checkin(sid, direction, window, sig):
    s = Seance.query.get_or_404(sid)
    if direction not in ("in", "out") or not hmac.compare_digest(sig, room_sig(s.id, direction, window)):
        abort(404)
    age = room_window() - window
    expired = age < 0 or age > (2 if request.method == "GET" else 8)
    result = None
    if request.method == "POST" and not expired:
        contact = request.form.get("contact", "")
        enr = find_by_contact(s.formation, contact)
        if not enr:
            result = ("error", scan_msg("unknown_email"))
        else:
            ok, key, t = do_checkin(s, enr, direction, "room")
            result = ("success" if ok else "error", f"{enr.student.full_name} — {scan_msg(key, t)}")
            if ok:
                session["ck_contact"] = contact
    return render_template("checkin.html", s=s, fo=s.formation, direction=direction, expired=expired,
                           result=result, contact=session.get("ck_contact", ""))


@app.route("/f/<slug>/presence", methods=["GET", "POST"])
def code_presence(slug):
    """Online séances: email/phone + the code announced by the trainer."""
    fo = Formation.query.filter_by(slug=slug).first_or_404()
    result = None
    if request.method == "POST":
        enr = find_by_contact(fo, request.form.get("contact"))
        code = re.sub(r"\D", "", request.form.get("code", ""))[:4]
        s, direction = None, None
        for cand in fo.seances.filter_by(day=today()):
            if code and cand.code_in == code:
                s, direction = cand, "in"
            elif code and cand.code_out == code and fo.check_mode == "inout":
                s, direction = cand, "out"
        if not enr:
            result = ("error", scan_msg("unknown_email"))
        elif not s:
            result = ("error", scan_msg("bad_code"))
        else:
            ok, key, t = do_checkin(s, enr, direction, "code")
            result = ("success" if ok else "error", f"{enr.student.full_name} — {scan_msg(key, t)}")
            if ok:
                session["ck_contact"] = request.form.get("contact", "")
    return render_template("code_presence.html", fo=fo, result=result, contact=session.get("ck_contact", ""))


# ── Payments ──────────────────────────────────────────────────────────────────────────────────────
@app.route("/formations/<int:fid>/paiements")
@role_required("manage")
def payments_view(fid):
    fo = Formation.query.get_or_404(fid)
    rows, tot = [], {"expected": 0, "paid": 0, "remaining": 0, "overdue": 0}
    for e in fo.enrollments.filter_by(status="confirmed").join(Student).order_by(Student.last_name):
        m = enrollment_money(e)
        rows.append({"enr": e, **m})
        for k in tot:
            tot[k] += m[k]
    return render_template("payments.html", fo=fo, rows=rows, tot=tot, state=payment_state, wa=wa_link,
                           base=base_url())


@app.route("/paiements/<int:pid>", methods=["POST"])
@role_required("manage")
def payment_action(pid):
    p = Payment.query.get_or_404(pid)
    enr = p.enrollment
    action = request.form.get("action")
    if action == "paid":
        m = request.form.get("method", "cash")
        p.method = m if m in PAY_METHODS else "autre"
        p.paid_on = parse_date(request.form.get("paid_on")) or today()
        amount = to_int(request.form.get("amount"), 0, 10_000_000)
        if 0 < amount < p.amount:   # partial payment: split the remainder into a new instalment
            nxt = (db.session.query(func.max(Payment.number)).filter_by(enrollment_id=enr.id).scalar() or 0) + 1
            db.session.add(Payment(enrollment_id=enr.id, number=nxt, label=f"Reliquat {p.label or ''}".strip()[:60],
                                   due_on=p.due_on, amount=p.amount - amount))
            p.amount = amount
        p.status = "paid"
        p.collected_by = current_user.name
        db.session.flush()
        p.receipt_no = f"R{p.paid_on.year}-{p.id:05d}"
        log("payment_paid", f"{enr.student.full_name} {fmt_da(p.amount)}")
        db.session.commit()
        if request.form.get("notify"):
            mail_payment(p, "received")
        flash(f"Paiement enregistré — reçu {p.receipt_no}.", "success")
        if request.form.get("print"):
            return redirect(url_for("receipt", pid=p.id))
    elif action == "undo":
        p.status, p.paid_on, p.method, p.receipt_no, p.collected_by = "due", None, None, None, None
        log("payment_undo", f"{enr.student.full_name} #{p.number}")
        db.session.commit()
    elif action == "waive":
        p.status = "waived"
        log("payment_waived", f"{enr.student.full_name} #{p.number}")
        db.session.commit()
    elif action == "edit":
        p.amount = to_int(request.form.get("amount"), 0, 10_000_000) or p.amount
        p.due_on = parse_date(request.form.get("due_on")) or p.due_on
        db.session.commit()
    elif action == "remind":
        ok = mail_payment(p, "manual")
        flash("Relance envoyée par email." if ok else "Aucun email envoyé (adresse ou configuration manquante).",
              "success" if ok else "error")
    return redirect(request.referrer or url_for("payments_view", fid=enr.formation_id))


@app.route("/inscriptions/<int:eid>/echeancier", methods=["POST"])
@role_required("manage")
def regen_schedule(eid):
    enr = Enrollment.query.get_or_404(eid)
    if enr.payments.filter(Payment.status != "due").count():
        flash("Impossible : des paiements sont déjà enregistrés.", "error")
    else:
        ensure_schedule(enr, force=True)
        flash("Échéancier recalculé.", "success")
    return redirect(url_for("payments_view", fid=enr.formation_id) + f"#e{enr.id}")


@app.route("/paiements/<int:pid>/recu")
@role_required("manage")
def receipt(pid):
    p = Payment.query.filter_by(id=pid, status="paid").first_or_404()
    return render_template("receipt.html", p=p, enr=p.enrollment, st=p.enrollment.student, fo=p.enrollment.formation,
                           money=enrollment_money(p.enrollment))


@app.route("/caisse")
@role_required("manage")
def cashbox():
    d1 = parse_date(request.args.get("du")) or today().replace(day=1)
    d2 = parse_date(request.args.get("au")) or today()
    pays = (Payment.query.filter(Payment.status == "paid", Payment.paid_on >= d1, Payment.paid_on <= d2)
            .order_by(Payment.paid_on.desc(), Payment.id.desc()).all())
    by_method = {}
    for p in pays:
        by_method[p.method or "autre"] = by_method.get(p.method or "autre", 0) + p.amount
    if request.args.get("export"):
        rows = [["Date", "Reçu", "Stagiaire", "Formation", "Libellé", "Mode", "Montant (DA)", "Encaissé par"]]
        for p in pays:
            rows.append([p.paid_on.strftime("%d/%m/%Y"), p.receipt_no, p.enrollment.student.full_name,
                         p.enrollment.formation.title, p.label or "", LABELS.get(p.method, p.method), p.amount,
                         p.collected_by or ""])
        return xlsx(rows, f"caisse_{d1}_{d2}.xlsx")
    return render_template("cashbox.html", pays=pays, d1=d1, d2=d2, total=sum(p.amount for p in pays), by_method=by_method)


def mail_payment(p, kind):
    """Email the student and the parent about an instalment (reminder or receipt)."""
    enr, st, fo, c = p.enrollment, p.enrollment.student, p.enrollment.formation, Setting.get("centre_name")
    link = f"{base_url()}/t/{enr.token}"
    info = fo.payment_info or Setting.get("payment_info")
    if kind == "received":
        title = "Paiement reçu — merci"
        paras = [f"Nous confirmons la réception de <b>{fmt_da(p.amount)}</b> ({p.label}) pour la formation "
                 f"<b>{fo.title}</b>, le {p.paid_on.strftime('%d/%m/%Y')}. Reçu n° {p.receipt_no}."]
    else:
        days = (p.due_on - today()).days
        when = (f"arrive à échéance le {p.due_on.strftime('%d/%m/%Y')}" if days > 0 else
                "est à régler aujourd'hui" if days == 0 else
                f"est en retard depuis le {p.due_on.strftime('%d/%m/%Y')}")
        title = "Rappel de paiement" if days >= 0 else "Paiement en retard"
        paras = [f"Le paiement <b>{p.label}</b> de <b>{fmt_da(p.amount)}</b> pour la formation <b>{fo.title}</b> "
                 f"({st.first_name} {st.last_name}) {when}.", info]
    sent = False
    for to, hello in ((st.email, f"Bonjour {st.first_name},"), (st.parent_email, f"Bonjour {st.parent_name or ''},".replace(" ,", ","))):
        if to:
            body = [hello] + paras
            text = "\n".join(re.sub("<[^>]+>", "", x) for x in body) + f"\n{link}"
            sent = send_email(to, f"{title} — {fo.title}", text, html_mail(c, title, body, ("Voir le détail", link)), c) or sent
    return sent


REMINDER_STAGES = ("b3", "d0", "a3", "a10")


def reminder_stage(p, d):
    days = (p.due_on - d).days
    if days > 3:
        return None
    return "b3" if days > 0 else "d0" if days > -3 else "a3" if days > -10 else "a10"


def run_payment_reminders():
    with app.app_context():
        now = local_now()
        if not 9 <= now.hour < 20:
            return 0
        sent = 0
        q = (Payment.query.join(Enrollment).join(Formation)
             .filter(Payment.status == "due", Enrollment.status == "confirmed", Formation.status != "draft",
                     Payment.due_on <= now.date() + timedelta(days=3)))
        for p in q.limit(200).all():
            stage = reminder_stage(p, now.date())
            if not stage or stage in (p.reminders or "").split(","):
                continue
            with app.test_request_context(base_url=APP_URL or "http://localhost"):
                ok = mail_payment(p, stage)
            if ok:
                p.reminders = ",".join(REMINDER_STAGES[:REMINDER_STAGES.index(stage) + 1])
                sent += 1
        db.session.commit()
        return sent


def run_seance_reminders():
    """J-1 email with time, place and personal space link."""
    with app.app_context():
        if local_now().hour < 9:
            return 0
        tomorrow = today() + timedelta(days=1)
        sent = 0
        c = Setting.get("centre_name")
        for s in Seance.query.filter_by(day=tomorrow, reminder_sent=False).all():
            fo = s.formation
            if fo.status in ("open", "running"):
                for e in fo.enrollments.filter_by(status="confirmed"):
                    st = e.student
                    if not st.email:
                        continue
                    link = f"{APP_URL}/t/{e.token}"
                    where = fo.online_url if fo.mode == "online" else (fo.location or "")
                    paras = [f"Bonjour {st.first_name},", f"Rappel : séance de <b>{fo.title}</b> demain "
                             f"{tomorrow.strftime('%d/%m/%Y')} {s.time_label or fo.default_time_label()}"
                             + (f" — {where}" if where else "") + ".",
                             "Présentez votre QR code (dans votre espace) à l'entrée."]
                    if send_email(st.email, f"Rappel séance — {fo.title}", "\n".join(re.sub("<[^>]+>", "", x) for x in paras) + "\n" + link,
                                  html_mail(c, "Rappel de séance", paras, ("Mon espace", link)), c):
                        sent += 1
            s.reminder_sent = True
        db.session.commit()
        return sent


# ── Students ──────────────────────────────────────────────────────────────────────────────────────
@app.route("/stagiaires", methods=["GET", "POST"])
@role_required("manage")
def students():
    if request.method == "POST":
        f = request.form
        if not f.get("last_name", "").strip() or not f.get("first_name", "").strip():
            flash("Nom et prénom requis.", "error")
            return redirect(url_for("students"))
        st = Student(first_name="", last_name="")
        db.session.add(st)
        apply_student_form(st, f)
        log("student_created", st.full_name)
        db.session.commit()
        return redirect(url_for("student_detail", sid=st.id))
    q = request.args.get("q", "").strip()
    query = Student.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Student.last_name.ilike(like), Student.first_name.ilike(like),
                                 Student.email.ilike(like), Student.phone.ilike(like), Student.parent_phone.ilike(like)))
    items = query.order_by(Student.last_name, Student.first_name).limit(300).all()
    return render_template("students.html", items=items, q=q)


@app.route("/stagiaires/<int:sid>", methods=["GET", "POST"])
@role_required("manage")
def student_detail(sid):
    st = Student.query.get_or_404(sid)
    if request.method == "POST":
        if request.form.get("action") == "delete":
            if st.enrollments.join(Payment).filter(Payment.status == "paid").count():
                flash("Suppression impossible : ce stagiaire a des paiements enregistrés.", "error")
                return redirect(url_for("student_detail", sid=st.id))
            log("student_deleted", st.full_name)
            db.session.delete(st)
            db.session.commit()
            return redirect(url_for("students"))
        if request.form.get("action") == "new_parent_link":
            st.parent_token = secrets.token_urlsafe(24)
        else:
            apply_student_form(st, request.form)
        db.session.commit()
        flash("Fiche enregistrée.", "success")
        return redirect(url_for("student_detail", sid=st.id))
    data = []
    for e in st.enrollments:
        row = enrollment_stats(e)
        data.append({"enr": e, "att": row, "money": enrollment_money(e), "cert": cert_eligible(e, row)})
    open_formations = Formation.query.filter(Formation.status.in_(("open", "running", "draft"))).all()
    return render_template("student_detail.html", st=st, data=data, open_formations=open_formations,
                           parent_url=f"{base_url()}/parent/{st.parent_token}", wa=wa_link, state=payment_state)


@app.route("/api/stagiaires")
@role_required("manage")
def students_api():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    items = Student.query.filter(or_(Student.last_name.ilike(like), Student.first_name.ilike(like),
                                     Student.phone.ilike(like), Student.email.ilike(like))).limit(10).all()
    return jsonify([{"id": s.id, "label": f"{s.full_name} · {s.phone or s.email or ''}"} for s in items])


# ── Certificates ──────────────────────────────────────────────────────────────────────────────────
def certificate_response(enr):
    from pdfs import certificate_pdf
    row = enrollment_stats(enr)
    if not enr.cert_no:
        enr.cert_issued_on = today()
        db.session.flush()
        enr.cert_no = f"ATT-{enr.cert_issued_on.year}-{enr.id:05d}"
        db.session.commit()
    pdf = certificate_pdf(Setting.all(), enr.student, enr.formation, enr, row, f"{base_url()}/verifier/{enr.token}")
    name = re.sub(r"[^A-Za-z0-9]+", "_", f"attestation_{enr.student.last_name}_{enr.student.first_name}")
    return send_file(io.BytesIO(pdf), mimetype="application/pdf", download_name=f"{name}.pdf")


@app.route("/inscriptions/<int:eid>/attestation")
@role_required("manage")
def certificate_admin(eid):
    enr = Enrollment.query.get_or_404(eid)
    ok, why = cert_eligible(enr)
    if not ok and not request.args.get("force"):
        flash(f"Attestation non disponible : {why}.", "error")
        return redirect(request.referrer or url_for("formation_detail", fid=enr.formation_id))
    if not ok:
        log("certificate_forced", f"{enr.student.full_name} ({why})")
    return certificate_response(enr)


@app.route("/t/<token>/attestation.pdf")
def certificate_public(token):
    enr = Enrollment.query.filter_by(token=token).first_or_404()
    if not cert_eligible(enr)[0] or enr.formation.status != "done":
        abort(404)
    return certificate_response(enr)


@app.route("/verifier/<token>")
def verify(token):
    enr = Enrollment.query.filter_by(token=token).first()
    return render_template("verify.html", enr=enr if enr and enr.cert_no else None)


# ── Public pages: catalogue, registration, student space, parent space ───────────────────────────
@app.route("/f")
def catalogue():
    items = Formation.query.filter(Formation.status.in_(("open", "running"))).order_by(Formation.starts_on).all()
    return render_template("catalogue.html", items=items)


@app.route("/f/<slug>", methods=["GET", "POST"])
def public_form(slug):
    fo = Formation.query.filter_by(slug=slug).first_or_404()
    if fo.status == "draft" and not current_user.is_authenticated:
        abort(404)
    done = None
    if request.method == "POST" and fo.status == "open":
        f = request.form
        if f.get("website"):     # honeypot
            return redirect(url_for("public_form", slug=slug))
        phone = re.sub(r"\D", "", f.get("phone", ""))
        email = f.get("email", "").strip().lower()
        if not f.get("first_name", "").strip() or not f.get("last_name", "").strip() or len(phone) < 9 \
                or (email and not _EMAIL_RE.match(email)) or not f.get("consent"):
            flash("Merci de remplir les champs obligatoires (nom, prénom, téléphone valide, accord).", "error")
            return render_template("public_form.html", fo=fo, form=f, done=None)
        st = find_or_create_student(f)
        db.session.flush()
        enr = fo.enrollments.filter_by(student_id=st.id).first()
        if enr and enr.status != "cancelled":
            return render_template("public_form.html", fo=fo, form={}, done="duplicate", enr=enr)
        left = fo.seats_left()
        status = "waitlist" if left is not None and left <= 0 else ("confirmed" if fo.auto_confirm else "pending")
        if not enr:
            enr = Enrollment(formation_id=fo.id, student_id=st.id, source=(request.args.get("src") or "web")[:40])
            db.session.add(enr)
        enr.status = "pending" if status == "confirmed" else status
        db.session.commit()
        if status == "confirmed":
            confirm_enrollment(enr)
        else:
            send_enrollment_mail(enr)
        return render_template("public_form.html", fo=fo, form={}, done=enr.status, enr=enr)
    return render_template("public_form.html", fo=fo, form={}, done=done, seances=fo.seances.all())


@app.route("/t/<token>")
def ticket(token):
    enr = Enrollment.query.filter_by(token=token).first_or_404()
    fo = enr.formation
    row = enrollment_stats(enr)
    qr = qr_svg("CF1:" + enr.token, 7) if enr.status == "confirmed" else ""
    return render_template("ticket.html", enr=enr, st=enr.student, fo=fo, qr=qr, row=row,
                           seances=fo.seances.all(), money=enrollment_money(enr), state=payment_state,
                           cert=cert_eligible(enr, row), today_seance=fo.seances.filter_by(day=today()).first())


@app.route("/t/<token>/code", methods=["POST"])
def ticket_code(token):
    enr = Enrollment.query.filter_by(token=token).first_or_404()
    code = re.sub(r"\D", "", request.form.get("code", ""))[:4]
    s, direction = None, None
    for cand in enr.formation.seances.filter_by(day=today()):
        if code and cand.code_in == code:
            s, direction = cand, "in"
        elif code and cand.code_out == code:
            s, direction = cand, "out"
    if not s:
        flash(scan_msg("bad_code"), "error")
    else:
        ok, key, t = do_checkin(s, enr, direction, "code")
        flash(scan_msg(key, t), "success" if ok else "error")
    return redirect(url_for("ticket", token=token))


@app.route("/parent/<token>")
def parent_space(token):
    st = Student.query.filter_by(parent_token=token).first_or_404()
    data = []
    for e in st.enrollments.filter(Enrollment.status != "cancelled"):
        row = enrollment_stats(e)
        fo = e.formation
        absences = [(s, row["marks"][s.id]) for s in fo.seances.all()
                    if s.id in row["marks"] and row["marks"][s.id].status in ("absent", "late", "excused")]
        data.append({"enr": e, "fo": fo, "row": row, "money": enrollment_money(e), "absences": absences,
                     "cert": cert_eligible(e, row),
                     "next": fo.seances.filter(Seance.day >= today()).limit(5).all()})
    return render_template("parent.html", st=st, data=data, state=payment_state)


# ── Exports ───────────────────────────────────────────────────────────────────────────────────────
def xlsx(rows, filename, widths=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0B2545")
    for i, col in enumerate(ws.columns):
        ws.column_dimensions[col[0].column_letter].width = (widths[i] if widths and i < len(widths)
                                                            else min(40, max(10, max(len(str(c.value or "")) for c in col) + 2)))
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/formations/<int:fid>/export/<kind>.xlsx")
@role_required("manage")
def export(fid, kind):
    fo = Formation.query.get_or_404(fid)
    safe = fo.slug[:40]
    if kind == "inscrits":
        rows = [["Nom", "Prénom", "Téléphone", "Email", "Date de naissance", "Niveau", "Parent", "Tél. parent", "Statut", "Inscrit le"]]
        for e in fo.enrollments:
            s = e.student
            rows.append([s.last_name, s.first_name, s.phone or "", s.email or "", s.birth_date.strftime("%d/%m/%Y") if s.birth_date else "",
                         s.level or "", s.parent_name or "", s.parent_phone or "", LABELS[e.status], e.created_at.strftime("%d/%m/%Y")])
        return xlsx(rows, f"inscrits_{safe}.xlsx")
    if kind == "presences":
        st = attendance_stats(fo)
        rows = [["Nom", "Prénom"] + [s.day.strftime("%d/%m") for s in st["held"]] + ["Taux %", "Heures", "Retards"]]
        for r in st["rows"]:
            cells = [LABELS.get(r["marks"][s.id].status, "") if s.id in r["marks"] and r["marks"][s.id].status else ""
                     for s in st["held"]]
            rows.append([r["enr"].student.last_name, r["enr"].student.first_name] + cells + [r["rate"], r["hours"], r["late"]])
        return xlsx(rows, f"presences_{safe}.xlsx")
    if kind == "paiements":
        rows = [["Nom", "Prénom", "Téléphone", "Attendu", "Payé", "Reste", "En retard"]]
        for e in fo.enrollments.filter_by(status="confirmed"):
            m = enrollment_money(e)
            rows.append([e.student.last_name, e.student.first_name, e.student.phone or "", m["expected"], m["paid"],
                         m["remaining"], m["overdue"]])
        return xlsx(rows, f"paiements_{safe}.xlsx")
    abort(404)


# ── Settings & users ──────────────────────────────────────────────────────────────────────────────
@app.route("/parametres", methods=["GET", "POST"])
@role_required("admin")
def settings():
    if request.method == "POST":
        for k in Setting.DEFAULTS:
            Setting.set(k, request.form.get(k, "").strip()[:1000])
        log("settings_saved")
        db.session.commit()
        flash("Paramètres enregistrés.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", s=Setting.all(),
                           email_ok=bool(os.environ.get("BREVO_API_KEY") or os.environ.get("SMTP_HOST")))


@app.route("/utilisateurs", methods=["GET", "POST"])
@role_required("admin")
def users():
    if request.method == "POST":
        f = request.form
        if f.get("action") == "toggle":
            u = User.query.get_or_404(f.get("user_id", type=int))
            if u.id != current_user.id:
                u.active = not u.active
        elif f.get("action") == "reset":
            u = User.query.get_or_404(f.get("user_id", type=int))
            pw = secrets.token_urlsafe(8)
            u.set_password(pw)
            flash(f"Nouveau mot de passe de {u.name} : {pw}", "success")
        else:
            email = f.get("email", "").strip().lower()
            if not _EMAIL_RE.match(email) or User.query.filter_by(email=email).first() or not f.get("name", "").strip():
                flash("Nom et email (unique) requis.", "error")
                return redirect(url_for("users"))
            pw = f.get("password") or secrets.token_urlsafe(8)
            u = User(name=f["name"].strip()[:120], email=email, role=f.get("role") if f.get("role") in ROLES else "secretaire")
            u.set_password(pw)
            db.session.add(u)
            flash(f"Compte créé. Mot de passe : {pw}", "success")
        log("users_changed")
        db.session.commit()
        return redirect(url_for("users"))
    return render_template("users.html", items=User.query.order_by(User.name).all(),
                           logs=AuditLog.query.order_by(AuditLog.id.desc()).limit(50).all())


@app.route("/mon-compte", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        if not current_user.check_password(request.form.get("old", "")):
            flash("Mot de passe actuel incorrect.", "error")
        elif len(request.form.get("new", "")) < 8:
            flash("8 caractères minimum.", "error")
        else:
            current_user.set_password(request.form["new"])
            db.session.commit()
            flash("Mot de passe modifié.", "success")
        return redirect(url_for("account"))
    return render_template("account.html")


@app.route("/health")
def health():
    return "ok"


@app.errorhandler(403)
def forbidden(_e):
    return render_template("message.html", title="Accès refusé", text="Vous n'avez pas accès à cette page."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("message.html", title="Page introuvable", text="Ce lien n'existe pas ou n'est plus valide."), 404


# ── Background jobs (one gunicorn worker, see Procfile) ───────────────────────────────────────────
def start_scheduler():
    if os.environ.get("DISABLE_SCHEDULER"):
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    sch = BackgroundScheduler(daemon=True)
    sch.add_job(run_payment_reminders, "interval", minutes=60, id="pay", next_run_time=datetime.now() + timedelta(minutes=2))
    sch.add_job(run_seance_reminders, "interval", minutes=60, id="seance", next_run_time=datetime.now() + timedelta(minutes=3))
    sch.start()


if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("ENABLE_SCHEDULER"):
    start_scheduler()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
