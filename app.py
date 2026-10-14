"""Centre de formation — gestion des formations, inscriptions, séances, présences (QR), paiements,
relances, attestations et espace parents. Flask + SQLAlchemy, prêt pour Railway."""
import calendar
from urllib.parse import urlencode
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

from flask import g
from models import (CASH_IN, CASH_OUT, FORMATION_STATUSES, PAY_METHODS, PLANS, ROLES, Attendance, AuditLog, CashEntry,
                    Enrollment, Formation, LicenseKey, Payment, School, Seance, Setting, Student, User, db)
import tenancy
from tenancy import as_school, unscoped
from notify import html_mail, send_email, wa_link
from i18n import LANGS, Labels, gettext as _

# ── App setup ─────────────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)   # Railway terminates HTTPS in front of us
if not os.environ.get("SECRET_KEY"):
    print("⚠️  SECRET_KEY absente : les sessions et QR seront invalidés à chaque redémarrage.")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "dev-" + hashlib.sha256(os.urandom(16)).hexdigest()
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024        # uploads (Excel, logo) ≤ 5 Mo
_PROD = bool(os.environ.get("RAILWAY_ENVIRONMENT"))
app.config.update(SESSION_COOKIE_SECURE=_PROD, REMEMBER_COOKIE_SECURE=_PROD, SESSION_COOKIE_HTTPONLY=True,
                  REMEMBER_COOKIE_HTTPONLY=True, REMEMBER_COOKIE_DURATION=timedelta(days=14),
                  PERMANENT_SESSION_LIFETIME=timedelta(days=14))
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
LOCAL = os.environ.get("APP_MODE") == "local"            # installation sur le PC de l'école (offre à vie)
SALES_PHONE = os.environ.get("SALES_PHONE", "+213699102523")  # WhatsApp du vendeur (renouvellements)
UTC_OFFSET = int(os.environ.get("UTC_OFFSET_HOURS", "1"))   # Algérie = UTC+1

db.init_app(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Veuillez vous connecter."


@login_manager.user_loader
def load_user(uid):
    with unscoped():
        return db.session.get(User, int(uid))


def auto_migrate():
    """create_all() never adds columns to existing tables: add the missing ones (nullable) so a
    deploy with new fields never breaks the live database."""
    from sqlalchemy import inspect as sa_inspect, text
    insp = sa_inspect(db.engine)
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have:
                ddl = col.type.compile(dialect=db.engine.dialect)
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))
                print(f"🛠  Colonne ajoutée : {table.name}.{col.name}")


with app.app_context():
    db.create_all()
    auto_migrate()
    tenancy.bootstrap(db.engine, (datetime.utcnow() + timedelta(hours=UTC_OFFSET)).date())


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    if _PROD:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.endpoint not in ("static",) and resp.mimetype == "text/html":
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


_LOGIN_FAILS = {}   # ip -> [count, first_failure_ts]


_HITS = {}   # (bucket, ip) -> [timestamps]


def rate_limit(bucket, per_hour):
    """Small in-memory limiter for public POST endpoints (one gunicorn worker)."""
    def deco(fn):
        @wraps(fn)
        def inner(*a, **kw):
            if request.method == "POST":
                now = datetime.utcnow().timestamp()
                key = (bucket, request.remote_addr or "?")
                hits = [t for t in _HITS.get(key, []) if now - t < 3600]
                if len(hits) >= per_hour:
                    return render_template("message.html", title="Trop de tentatives",
                                           text="Merci de réessayer un peu plus tard."), 429
                hits.append(now)
                _HITS[key] = hits
            return fn(*a, **kw)
        return inner
    return deco


def _login_blocked(ip):
    n, t0 = _LOGIN_FAILS.get(ip, (0, 0))
    if n and datetime.utcnow().timestamp() - t0 > 900:
        _LOGIN_FAILS.pop(ip, None)
        return False
    return n >= 8


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
        with unscoped():
            q = Formation.query.filter_by(slug=slug)
        if exclude_id:
            q = q.filter(Formation.id != exclude_id)
            hit = q.first()
        if not hit:
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
                flash(_("Accès réservé."), "error")
                return redirect(url_for("dashboard"))
            return fn(*a, **kw)
        return inner
    return deco


LABELS = Labels({
    "draft": "Brouillon", "open": "Inscriptions ouvertes", "running": "En cours", "done": "Terminée",
    "pending": "En attente", "confirmed": "Confirmé", "waitlist": "Liste d'attente", "cancelled": "Annulé",
    "present": "Présent", "late": "Retard", "absent": "Absent", "excused": "Excusé",
    "due": "À payer", "paid": "Payé", "waived": "Exonéré", "overdue": "En retard",
    "cash": "Espèces", "ccp": "CCP", "virement": "Virement", "cheque": "Chèque", "autre": "Autre",
    "onsite": "Présentiel", "online": "En ligne", "hybrid": "Hybride",
    "admin": "Administrateur", "secretaire": "Secrétariat", "formateur": "Formateur",
    "ticket": "QR ticket", "room": "QR salle", "code": "Code", "manual": "Manuel",
    "in": "Entrée", "out": "Sortie", "fees": "Frais de formation",
    "salaire": "Salaires enseignants", "loyer": "Loyer", "energie": "Électricité, eau, gaz", "telecom": "Internet et téléphone",
    "fournitures": "Fournitures", "entretien": "Entretien et ménage", "publicite": "Publicité", "impots": "Impôts et CNAS",
    "transport": "Transport", "equipement": "Équipement", "autre_charge": "Autre charge",
    "autre_recette": "Autre recette", "subvention": "Subvention", "vente": "Vente (livres, tenues…)", "apport": "Apport du gérant",
    "hour": "À l'heure", "seance": "À la séance", "month": "Fixe mensuel",
})
L = LABELS


@app.context_processor
def inject():
    nav = {}
    if current_user.is_authenticated and current_user.can("manage"):
        nav["overdue"] = (Payment.query.join(Enrollment).join(Formation)
                          .filter(Payment.status == "due", Payment.due_on < today(), Enrollment.status == "confirmed",
                                  Formation.status != "draft").count())
        nav["pending"] = Enrollment.query.filter_by(status="pending").count()
    from flask import g
    lang = getattr(g, "lang", "fr")
    args = {k: v for k, v in request.args.items() if k != "lang"} if request else {}
    return {"_": _, "lang": lang, "dir": "rtl" if lang == "ar" else "ltr", "LANGS": LANGS,
            "lang_url": lambda code: request.path + "?" + urlencode({**args, "lang": code}),
            "nav": nav, "L": LABELS, "centre": centre_settings(), "today": today(), "fmt_da": fmt_da, "wa": wa_link,
            "email_on": bool(os.environ.get("BREVO_API_KEY") or os.environ.get("SMTP_HOST")),
            "roles": ROLES, "pay_methods": PAY_METHODS, "statuses": FORMATION_STATUSES,
            "school": g.get("school"), "lic": licence_info(g.get("school")), "LOCAL": LOCAL}


def centre_settings():
    if g.get("school_id"):
        return Setting.all()
    return {**Setting.DEFAULTS, "centre_name": "HaithemEduAI", "centre_tagline": "Gestion des écoles et centres de formation"}


def licence_info(sc):
    if not sc:
        return None
    left = sc.days_left(today())
    return {"plan": sc.plan, "left": left, "expired": sc.expired(today()),
            "warn": left is not None and (left <= 15 or sc.plan == "trial"),
            "renew_wa": wa_link(SALES_PHONE, _("Bonjour, je souhaite renouveler la licence HaithemEduAI de « {name} ».",
                                                name=sc.name))}


def plabel(label):
    """Translate a stored instalment label (« Mensualité 2/9 », « Reliquat … »)."""
    label = label or ""
    if label.startswith("Reliquat "):
        return _("Reliquat") + " " + plabel(label[9:])
    m = re.match(r"^Mensualité (\d+)/(\d+)$", label)
    if m:
        return _("Mensualité {k}/{n}", k=m[1], n=m[2])
    return _(label)


app.jinja_env.filters["plabel"] = plabel
app.jinja_env.filters["ltr"] = lambda v: ("\u2066" + str(v) + "\u2069") if v else ""   # phones, codes in Arabic pages
app.jinja_env.filters["da"] = lambda n: "\u2066" + fmt_da(n) + "\u2069"   # isolated: stays "12 000 DA" in Arabic pages
app.jinja_env.filters["d"] = lambda d: d.strftime("%d/%m/%Y") if d else ""
app.jinja_env.filters["hm"] = lambda dt: (dt + timedelta(hours=UTC_OFFSET)).strftime("%H:%M") if dt else ""


def qr_svg(text, scale=6):
    import segno
    return segno.make(text, error="m").svg_inline(scale=scale, dark="#0b2545", light="#ffffff", border=2)


# ── First run / auth ──────────────────────────────────────────────────────────────────────────────
@app.before_request
def pick_lang():
    from flask import g
    lang = request.args.get("lang")
    if lang in LANGS:
        session["lang"] = lang
        if current_user.is_authenticated and current_user.lang != lang:
            current_user.lang = lang
            db.session.commit()
    lang = session.get("lang") or (current_user.lang if current_user.is_authenticated else None)
    if lang not in LANGS:
        try:
            lang = Setting.get("default_lang")
        except Exception:  # noqa: BLE001
            lang = "fr"
    g.lang = lang if lang in LANGS else "fr"


# Public links resolve their school from the token / slug they carry.
_PUBLIC = {"scan_page": (Seance, "scan_token", "token"), "scan_check": (Seance, "scan_token", "token"),
           "room_display": (Seance, "room_token", "token"), "room_qr": (Seance, "room_token", "token"),
           "room_checkin": (Seance, "id", "sid"), "code_presence": (Formation, "slug", "slug"),
           "public_form": (Formation, "slug", "slug"), "certificate_public": (Enrollment, "token", "token"),
           "verify": (Enrollment, "token", "token"), "ticket": (Enrollment, "token", "token"),
           "ticket_code": (Enrollment, "token", "token"), "parent_space": (Student, "parent_token", "token"),
           "catalogue": (School, "slug", "sslug")}
_READONLY_OK = {"licence", "logout", "login", "account", "data_export", "signup", "static"}


def tenant_context():
    """Pick the current school: the signed-in user's, or the one a public link belongs to."""
    g.school_id, g.unscoped, g.school = None, False, None
    if request.path.startswith("/static"):
        return None
    if current_user.is_authenticated and request.endpoint != "catalogue":
        g.school_id = current_user.school_id
        if current_user.is_super and session.get("as_school"):
            g.school_id = session["as_school"]
        if current_user.is_super and not g.school_id and not LOCAL and request.endpoint and \
                not (request.endpoint.startswith("platform") or request.endpoint in ("logout", "account", "static", "login")):
            return redirect(url_for("platform"))
    elif request.endpoint in _PUBLIC and request.view_args:
        model, col, arg = _PUBLIC[request.endpoint]
        with unscoped():
            obj = model.query.filter(getattr(model, col) == request.view_args.get(arg)).first()
        if obj is not None:
            g.school_id = obj.id if model is School else obj.school_id
    elif request.endpoint == "catalogue_default" and LOCAL:
        with unscoped():
            first = School.query.order_by(School.id).first()
        g.school_id = first.id if first else None
    if g.school_id:
        with unscoped():
            g.school = db.session.get(School, g.school_id)
        sc = g.school
        if sc and not sc.active and current_user.is_authenticated and not current_user.is_super:
            logout_user()
            flash(_("Le compte de cette école est suspendu. Contactez-nous sur WhatsApp."), "error")
            return redirect(url_for("login"))
        if sc and sc.expired(today()) and request.method == "POST" and request.endpoint not in _READONLY_OK \
                and not (request.endpoint or "").startswith("platform"):
            if current_user.is_authenticated:
                flash(_("Licence expirée : l'application est en lecture seule. Renouvelez pour continuer."), "error")
                return redirect(url_for("licence"))
            return render_template("message.html", title=_("Service indisponible"),
                                   text=_("Ce service est momentanément indisponible. Contactez l'établissement.")), 403
    return None


app.before_request_funcs.setdefault(None, []).insert(0, tenant_context)


@app.before_request
def first_run():
    if request.endpoint in ("setup", "static", "health") or request.path.startswith("/static"):
        return None
    with unscoped():
        empty = not User.query.first()
    if empty:
        return redirect(url_for("setup"))
    return None


def create_school(name, admin_name, email, pw, phone="", city="", plan="trial", super_=False):
    sc = School(name=name[:160], slug=tenancy.school_slug(name), phone=phone[:30], city=city[:80])
    tenancy.apply_plan(sc, plan, today())
    db.session.add(sc)
    db.session.flush()
    with as_school(sc.id):
        u = User(name=admin_name[:120], email=email, role="admin", school_id=sc.id, is_super=super_ or None)
        u.set_password(pw)
        db.session.add(u)
        Setting.set("centre_name", name[:120])
        if phone:
            Setting.set("centre_phone", phone[:30])
        db.session.add(AuditLog(school_id=sc.id, user=admin_name[:120], action="school_created", details=plan))
        db.session.flush()
    return sc, u


def _signup_form_errors(f):
    email, pw = f.get("email", "").strip().lower(), f.get("password", "")
    if not _EMAIL_RE.match(email) or len(pw) < 8 or not f.get("name", "").strip() or not f.get("centre_name", "").strip():
        return _("Nom, établissement, email valide et mot de passe (8 caractères min.) requis.")
    with unscoped():
        if User.query.filter_by(email=email).first():
            return _("Cet email est déjà utilisé. Connectez-vous ou choisissez un autre email.")
    return None


@app.route("/setup", methods=["GET", "POST"])
def setup():
    with unscoped():
        if User.query.first():
            return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        err = _signup_form_errors(f)
        if err:
            flash(err, "error")
        else:
            # Online: the very first account is the platform owner. Local: 30-day trial until a key is entered.
            sc, u = create_school(f["centre_name"].strip(), f["name"].strip(), f["email"].strip().lower(),
                                  f["password"], plan="trial" if LOCAL else "lifetime", super_=not LOCAL)
            db.session.commit()
            login_user(u, remember=True)
            flash(_("Bienvenue ! Complétez les informations du centre."), "success")
            return redirect(url_for("settings"))
    return render_template("setup.html")


@app.route("/creer-mon-ecole", methods=["GET", "POST"])
@rate_limit("signup", 6)
def signup():
    if LOCAL:
        return redirect(url_for("login"))
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        f = request.form
        err = None if not f.get("website") else "bot"
        err = err or _signup_form_errors(f)
        if err:
            if err != "bot":
                flash(err, "error")
            return render_template("signup.html", form=f, plans=PLANS)
        sc, u = create_school(f["centre_name"].strip(), f["name"].strip(), f["email"].strip().lower(), f["password"],
                              phone=re.sub(r"[^\d+ ]", "", f.get("phone", "")), city=f.get("city", "").strip())
        db.session.commit()
        login_user(u, remember=True)
        flash(_("Votre école est créée : 30 jours d'essai gratuit, toutes les fonctions incluses."), "success")
        return redirect(url_for("settings"))
    return render_template("signup.html", form={}, plans=PLANS)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    ip = request.remote_addr or "?"
    if request.method == "POST":
        if _login_blocked(ip):
            flash(_("Trop de tentatives. Réessayez dans 15 minutes."), "error")
            return render_template("login.html"), 429
        with unscoped():
            u = User.query.filter_by(email=request.form.get("email", "").strip().lower()).first()
            sc = db.session.get(School, u.school_id) if u and u.school_id else None
        if u and u.active and u.check_password(request.form.get("password", "")):
            if sc and not sc.active and not u.is_super:
                flash(_("Le compte de cette école est suspendu. Contactez-nous sur WhatsApp."), "error")
                return render_template("login.html")
            _LOGIN_FAILS.pop(ip, None)
            login_user(u, remember=bool(request.form.get("remember")))
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        n, t0 = _LOGIN_FAILS.get(ip, (0, datetime.utcnow().timestamp()))
        _LOGIN_FAILS[ip] = (n + 1, t0)
        flash(_("Identifiants incorrects."), "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("as_school", None)
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
        return False, _("Inscription non confirmée")
    row = row or enrollment_stats(enr)
    if not row["eligible_att"]:
        return False, _("Assiduité insuffisante ({rate} % < {min} %)", rate=row["rate"], min=enr.formation.min_attendance)
    if enr.formation.cert_requires_payment and enrollment_money(enr)["remaining"] > 0:
        return False, _("Reste à payer")
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
    return _(SCAN_MSG.get(key, key), t=t)


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
@app.route("/accueil")
def landing():
    """Public home page: carousel, features, pricing, FAQ, sign-up (online mode)."""
    import landing_i18n as LI
    if LOCAL and not current_user.is_authenticated:
        return redirect(url_for("login"))
    lang = g.lang
    if not session.get("lang") and not request.args.get("lang"):
        lang = request.accept_languages.best_match(["fr", "ar", "en"]) or lang
    c = LI.T[lang]
    wa_plan = {k: wa_link(SALES_PHONE, c["wa_plan"].format(plan=p[0])) for p in c["plans"] for k in [p[1]]}
    return render_template("landing.html", c=c, lang=lang, langs=LANGS, hero=LI.HERO, phones=LI.PHONES, tabs=LI.TABS,
                           wa_plan=wa_plan, wa=wa_link(SALES_PHONE, c["wa_text"]), year=today().year)


@app.route("/")
def dashboard():
    if not current_user.is_authenticated:
        return landing()
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
    # collections over the last 6 months
    months = []
    m0 = today().replace(day=1)
    for k in range(5, -1, -1):
        a = add_months(m0, -k)
        b = add_months(a, 1)
        v = db.session.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
            Payment.status == "paid", Payment.paid_on >= a, Payment.paid_on < b).scalar()
        months.append({"label": mshort(a.month),
                       "value": int(v or 0)})
    mmax = max([m["value"] for m in months] + [1])
    running = []
    for fo in Formation.query.filter(Formation.status.in_(("open", "running"))).order_by(Formation.starts_on).all():
        st = attendance_stats(fo)
        nxt = fo.seances.filter(Seance.day >= today()).first()
        running.append({"fo": fo, "avg": st["avg"], "held": len(st["held"]), "total": len(st["seances"]),
                        "confirmed": len(st["rows"]), "next": nxt})
    setup = {"settings": bool(Setting.get("centre_address") or Setting.get("centre_phone")),
             "formation": Formation.query.count() > 0, "seances": Seance.query.count() > 0,
             "open": Formation.query.filter(Formation.status != "draft").count() > 0,
             "students": Enrollment.query.count() > 0}
    due_week = (db.session.query(func.coalesce(func.sum(Payment.amount), 0)).join(Enrollment)
                .filter(Payment.status == "due", Enrollment.status == "confirmed",
                        Payment.due_on >= today(), Payment.due_on <= today() + timedelta(days=7)).scalar())
    return render_template("dashboard.html", kpi=kpi, overdue=overdue, seances_today=seances_today,
                           upcoming=upcoming, pending=pending, state=payment_state, months=months, mmax=mmax,
                           running=running, alerts=absence_alerts()[:12], setup=setup, due_week=due_week)


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
            flash(_("Le titre est obligatoire."), "error")
            return redirect(url_for("formations"))
        fo = Formation(title=title[:160], slug=slugify(title))
        apply_formation_form(fo, request.form)
        db.session.add(fo)
        log("formation_created", fo.title)
        db.session.commit()
        flash(_("Formation créée. Ajoutez maintenant les séances."), "success")
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
        flash(_("Formation enregistrée."), "success")
        tab = "infos"
    elif action == "status" and f.get("status") in FORMATION_STATUSES:
        fo.status = f["status"]
        log("formation_status", f"{fo.title} → {fo.status}")
        db.session.commit()
        flash(_("Statut mis à jour."), "success")
    elif action in ("add_seance", "gen_seances"):
        tab = "seances"
        d = parse_date(f.get("day"))
        a, b = hm_to_min(f.get("start_time")), hm_to_min(f.get("end_time"))
        label = f"{f['start_time'].strip()} - {f['end_time'].strip()}" if a is not None and b is not None and b > a else None
        if not d:
            flash(_("Date invalide."), "error")
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
            flash(_("{n} séances créées.", n=created), "success")
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
                flash(_("Nom et prénom requis."), "error")
                return redirect(url_for("formation_detail", fid=fo.id))
            st = find_or_create_student(f)
            db.session.flush()
        if fo.enrollments.filter_by(student_id=st.id).first():
            flash(_("Ce stagiaire est déjà inscrit."), "error")
        else:
            enr = Enrollment(formation_id=fo.id, student_id=st.id, source="secretariat",
                             discount=to_int(f.get("discount"), 0, 10_000_000))
            db.session.add(enr)
            db.session.commit()
            confirm_enrollment(enr)
            log("enrollment_added", f"{st.full_name} → {fo.title}")
            db.session.commit()
            flash(_("{name} inscrit(e).", name=st.full_name), "success")
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
                flash(_("Remise enregistrée. Des paiements existent déjà : échéancier non recalculé."), "error")
            else:
                ensure_schedule(enr, force=True)
                flash(_("Remise appliquée, échéancier recalculé."), "success")
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
            flash(_("Nouveaux codes générés."), "success")
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
        flash(_("Présences enregistrées."), "success")
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
@rate_limit("checkin", 120)
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
@rate_limit("code", 60)
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
        flash(_("Paiement enregistré — reçu {no}.", no=p.receipt_no), "success")
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
        flash(_("Relance envoyée par email.") if ok else "Aucun email envoyé (adresse ou configuration manquante).",
              "success" if ok else "error")
    return redirect(request.referrer or url_for("payments_view", fid=enr.formation_id))


@app.route("/inscriptions/<int:eid>/echeancier", methods=["POST"])
@role_required("manage")
def regen_schedule(eid):
    enr = Enrollment.query.get_or_404(eid)
    if enr.payments.filter(Payment.status != "due").count():
        flash(_("Impossible : des paiements sont déjà enregistrés."), "error")
    else:
        ensure_schedule(enr, force=True)
        flash(_("Échéancier recalculé."), "success")
    return redirect(url_for("payments_view", fid=enr.formation_id) + f"#e{enr.id}")


@app.route("/paiements/<int:pid>/recu")
@role_required("manage")
def receipt(pid):
    p = Payment.query.filter_by(id=pid, status="paid").first_or_404()
    return render_template("receipt.html", p=p, enr=p.enrollment, st=p.enrollment.student, fo=p.enrollment.formation,
                           money=enrollment_money(p.enrollment))


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


def _run_payment_reminders():
    if True:
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


def _run_seance_reminders():
    """J-1 email with time, place and personal space link."""
    if True:
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


def _each_school(fn):
    """Run a background job once per school whose licence is valid."""
    def job():
        with app.app_context():
            with unscoped():
                ids = [sc.id for sc in School.query.filter_by(active=True).all() if not sc.expired(today())]
            total = 0
            for sid in ids:
                with as_school(sid):
                    try:
                        total += fn() or 0
                    except Exception as e:  # noqa: BLE001
                        db.session.rollback()
                        print("job", fn.__name__, sid, e)
            return total
    job.__name__ = fn.__name__[1:]
    return job


run_payment_reminders = _each_school(_run_payment_reminders)
run_seance_reminders = _each_school(_run_seance_reminders)


# ── Students ──────────────────────────────────────────────────────────────────────────────────────
@app.route("/stagiaires", methods=["GET", "POST"])
@role_required("manage")
def students():
    if request.method == "POST":
        f = request.form
        if not f.get("last_name", "").strip() or not f.get("first_name", "").strip():
            flash(_("Nom et prénom requis."), "error")
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
                flash(_("Suppression impossible : ce stagiaire a des paiements enregistrés."), "error")
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
        flash(_("Fiche enregistrée."), "success")
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


# ── Excel import ──────────────────────────────────────────────────────────────────────────────────
IMPORT_COLS = [("last_name", "Nom"), ("first_name", "Prénom"), ("phone", "Téléphone"), ("email", "Email"),
               ("birth_date", "Date de naissance"), ("birth_place", "Lieu de naissance"), ("level", "Niveau"),
               ("address", "Adresse"), ("parent_name", "Parent / tuteur"), ("parent_phone", "Tél. parent"),
               ("parent_email", "Email parent")]
_ALIASES = {"nom": "last_name", "nom de famille": "last_name", "prenom": "first_name", "telephone": "phone",
            "tel": "phone", "mobile": "phone", "email": "email", "e-mail": "email", "mail": "email",
            "date de naissance": "birth_date", "naissance": "birth_date", "lieu de naissance": "birth_place",
            "niveau": "level", "niveau d'etudes": "level", "adresse": "address", "parent": "parent_name",
            "parent / tuteur": "parent_name", "tuteur": "parent_name", "nom du parent": "parent_name",
            "tel. parent": "parent_phone", "tel parent": "parent_phone", "telephone parent": "parent_phone",
            "telephone du parent": "parent_phone", "email parent": "parent_email"}


def _norm_head(h):
    import unicodedata
    h = unicodedata.normalize("NFKD", str(h or "")).encode("ascii", "ignore").decode().strip().lower()
    return re.sub(r"\s+", " ", h)


def _cell_str(v, field):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    v = str(v).strip()
    if field in ("phone", "parent_phone"):
        d = re.sub(r"\D", "", v)
        if len(d) == 9 and d[0] in "567":      # Excel dropped the leading 0
            d = "0" + d
        return d
    if field == "birth_date" and re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", v):
        dd, mm, yy = v.split("/")
        return f"{yy}-{int(mm):02d}-{int(dd):02d}"
    return v


def read_students_xlsx(fileobj):
    """Rows as dicts of student fields. Header row matched by name (accents/case ignored)."""
    from openpyxl import load_workbook
    ws = load_workbook(fileobj, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    labels = {_norm_head(lab): f for f, lab in IMPORT_COLS}
    mapping = {}
    for i, h in enumerate(rows[0]):
        key = _norm_head(h)
        f = labels.get(key) or _ALIASES.get(key)
        if f and f not in mapping.values():
            mapping[i] = f
    out = []
    for r in rows[1:]:
        d = {f: _cell_str(r[i] if i < len(r) else None, f) for i, f in mapping.items()}
        if any(d.values()):
            out.append(d)
    return out


@app.route("/import/modele.xlsx")
@role_required("manage")
def import_template():
    return xlsx([[lab for _, lab in IMPORT_COLS],
                 ["BENALI", "Amine", "0550123456", "amine@exemple.dz", "15/03/2005", "Sétif", "3AS", "", "Benali Karim",
                  "0661000000", ""]], "modele_import_stagiaires.xlsx")


@app.route("/import", methods=["POST"])
@role_required("manage")
def import_students():
    fid = to_int(request.form.get("formation_id"), 0, 10 ** 9)
    fo = db.session.get(Formation, fid) if fid else None
    back = url_for("formation_detail", fid=fo.id) if fo else url_for("students")
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        flash(_("Choisissez un fichier Excel (.xlsx)."), "error")
        return redirect(back)
    try:
        rows = read_students_xlsx(f)
    except Exception:  # noqa: BLE001
        flash(_("Fichier illisible. Utilisez le modèle fourni."), "error")
        return redirect(back)
    report = {"created": 0, "updated": 0, "enrolled": 0, "already": 0, "errors": []}
    for n, d in enumerate(rows, start=2):
        if not d.get("last_name") or not d.get("first_name"):
            report["errors"].append(_("ligne {n} : nom ou prénom manquant", n=n))
            continue
        d = {k: v for k, v in d.items() if v}       # empty cells never erase an existing file
        before = Student.query.count()
        st = find_or_create_student(d)
        db.session.flush()
        report["created" if Student.query.count() > before else "updated"] += 1
        if fo:
            if fo.enrollments.filter_by(student_id=st.id).first():
                report["already"] += 1
            else:
                enr = Enrollment(formation_id=fo.id, student_id=st.id, source="import", status="confirmed")
                db.session.add(enr)
                db.session.flush()
                report["enrolled"] += 1
    db.session.commit()
    if fo:
        for enr in fo.enrollments.filter_by(source="import", status="confirmed"):
            ensure_schedule(enr)
    log("import_excel", f"{len(rows)} lignes" + (f" → {fo.title}" if fo else ""))
    db.session.commit()
    msg = _("Import terminé : {a} nouveaux stagiaires, {b} déjà connus (fiches mises à jour)", a=report["created"], b=report["updated"])
    if fo:
        msg += _(", {n} inscrits à la formation", n=report["enrolled"]) + (_(", {n} déjà inscrits", n=report["already"]) if report["already"] else "")
    flash(msg + ".", "success")
    if report["errors"]:
        flash(_("Lignes ignorées — ") + " ; ".join(report["errors"][:15]), "error")
    return redirect(back)


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
        flash(_("Attestation non disponible : {why}.", why=why), "error")
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
def catalogue_default():
    if current_user.is_authenticated or (LOCAL and g.get("school_id")):
        return redirect(url_for("catalogue", sslug=g.school.slug)) if g.get("school") else abort(404)
    abort(404)


@app.route("/e/<sslug>")
def catalogue(sslug):
    if not g.get("school_id"):
        abort(404)
    items = Formation.query.filter(Formation.status.in_(("open", "running"))).order_by(Formation.starts_on).all()
    return render_template("catalogue.html", items=items)


@app.route("/f/<slug>", methods=["GET", "POST"])
@rate_limit("register", 20)
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
            flash(_("Merci de remplir les champs obligatoires (nom, prénom, téléphone valide, accord)."), "error")
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
@rate_limit("tcode", 30)
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


# ── Global search ─────────────────────────────────────────────────────────────────────────────────
@app.route("/recherche")
@role_required("manage")
def search():
    q = request.args.get("q", "").strip()
    res = {"students": [], "formations": [], "receipts": []}
    if len(q) >= 2:
        like = f"%{q}%"
        res["students"] = Student.query.filter(or_(
            Student.last_name.ilike(like), Student.first_name.ilike(like), Student.email.ilike(like),
            Student.phone.ilike(like), Student.parent_phone.ilike(like), Student.parent_name.ilike(like),
            (Student.last_name + " " + Student.first_name).ilike(like),
            (Student.first_name + " " + Student.last_name).ilike(like))).limit(30).all()
        res["formations"] = Formation.query.filter(Formation.title.ilike(like)).limit(15).all()
        res["receipts"] = Payment.query.filter(Payment.receipt_no.ilike(like)).limit(10).all()
        total = sum(len(v) for v in res.values())
        if total == 1:
            if res["students"]:
                return redirect(url_for("student_detail", sid=res["students"][0].id))
            if res["formations"]:
                return redirect(url_for("formation_detail", fid=res["formations"][0].id))
            return redirect(url_for("receipt", pid=res["receipts"][0].id))
    return render_template("search.html", q=q, res=res)


# ── Unpaid follow-up across all formations, with a WhatsApp queue ─────────────────────────────────
def pay_wa_message(p):
    enr, st, fo = p.enrollment, p.enrollment.student, p.enrollment.formation
    d = (p.due_on - today()).days
    when = (_("arrive à échéance le {d}", d=p.due_on.strftime("%d/%m/%Y")) if d > 0 else _("est à régler aujourd'hui") if d == 0
            else _("était dû le {d}", d=p.due_on.strftime("%d/%m/%Y")))
    return _("Bonjour, {centre} vous rappelle que le paiement « {label} » de {amount} pour la formation {title} ({name}) {when}. Détail : {url}",
             centre=Setting.get("centre_name"), label=plabel(p.label), amount=fmt_da(p.amount), title=fo.title,
             name=f"{st.first_name} {st.last_name}", when=when, url=f"{base_url()}/t/{enr.token}") + \
        "\n" + (fo.payment_info or Setting.get("payment_info"))


@app.route("/impayes")
@role_required("manage")
def unpaid():
    horizon = to_int(request.args.get("jours", 7), 0, 60)
    fid = to_int(request.args.get("formation"), 0, 10 ** 9)
    q = (Payment.query.join(Enrollment).join(Formation)
         .filter(Payment.status == "due", Enrollment.status == "confirmed", Formation.status != "draft",
                 Payment.due_on <= today() + timedelta(days=horizon)))
    if fid:
        q = q.filter(Enrollment.formation_id == fid)
    pays = q.order_by(Payment.due_on).all()
    total = sum(p.amount for p in pays)
    overdue = sum(p.amount for p in pays if p.due_on < today())
    return render_template("unpaid.html", pays=pays, total=total, overdue=overdue, horizon=horizon, fid=fid,
                           formations=Formation.query.filter(Formation.is_free.is_(False)).order_by(Formation.title).all(),
                           state=payment_state)


@app.route("/paiements/<int:pid>/wa/<who>")
@role_required("manage")
def payment_wa(pid, who):
    """Log the WhatsApp reminder, then open WhatsApp with the prepared message."""
    p = Payment.query.get_or_404(pid)
    st = p.enrollment.student
    phone = st.parent_phone if who == "parent" else st.phone
    if not phone:
        abort(404)
    p.last_wa = datetime.utcnow()
    p.wa_count = (p.wa_count or 0) + 1
    log("payment_wa", f"{st.full_name} #{p.number} → {who}")
    db.session.commit()
    return redirect(wa_link(phone, pay_wa_message(p)))


# ── Absence alerts ────────────────────────────────────────────────────────────────────────────────
def absence_alerts(limit_formations=None):
    """Confirmed students absent at the last 2 held séances (sheet filled) or under the attendance threshold."""
    out = []
    q = Formation.query.filter(Formation.status.in_(("open", "running")))
    for fo in q.all():
        held = [s for s in fo.seances.filter(Seance.day <= today()).order_by(None).order_by(Seance.day.desc()).limit(6)
                if s.attendances.filter(Attendance.status.isnot(None)).count()]
        if not held:
            continue
        last2 = held[:2]
        for e in fo.enrollments.filter_by(status="confirmed"):
            marks = {a.seance_id: a.status for a in e.attendances}
            streak = 0
            for s in held:
                if marks.get(s.id) in (None, "absent"):
                    streak += 1
                else:
                    break
            if streak >= 2 and len(last2) == 2:
                out.append({"enr": e, "fo": fo, "streak": streak, "last": held[0].day})
    return sorted(out, key=lambda x: -x["streak"])


@app.route("/inscriptions/<int:eid>/wa-absence")
@role_required("manage")
def absence_wa(eid):
    e = Enrollment.query.get_or_404(eid)
    st = e.student
    phone = st.parent_phone or st.phone
    if not phone:
        abort(404)
    e.last_absence_alert = today()
    log("absence_wa", st.full_name)
    db.session.commit()
    msg = _("Bonjour{who}, {centre} vous informe que {name} a été absent(e) aux dernières séances de la formation « {title} ». Merci de nous contacter. Suivi : {url}",
            who=(" " + st.parent_name) if st.parent_name and st.parent_phone else "", centre=Setting.get("centre_name"),
            name=f"{st.first_name} {st.last_name}", title=e.formation.title, url=f"{base_url()}/parent/{st.parent_token}")
    return redirect(wa_link(phone, msg))


# ── Formation: duplicate, delete, sign-in sheet, certificates in bulk ────────────────────────────
_COPY_FIELDS = ("description", "programme", "prerequisites", "audience", "trainer", "trainer_id", "location", "mode",
                "online_url", "group_url", "start_time", "end_time", "hours_total", "seats", "is_free",
                "registration_fee", "price_total", "installments", "payment_info", "check_mode", "late_tolerance",
                "min_attendance", "cert_requires_payment", "auto_confirm")


@app.route("/formations/<int:fid>/dupliquer", methods=["POST"])
@role_required("manage")
def formation_duplicate(fid):
    src = Formation.query.get_or_404(fid)
    title = (request.form.get("title") or f"{src.title} (nouvelle session)").strip()[:160]
    fo = Formation(title=title, slug=slugify(title), status="draft")
    for f in _COPY_FIELDS:
        setattr(fo, f, getattr(src, f))
    start = parse_date(request.form.get("starts_on"))
    fo.starts_on = start
    db.session.add(fo)
    db.session.flush()
    old = src.seances.all()
    if start and old and request.form.get("copy_seances"):
        shift = start - old[0].day
        for s in old:
            db.session.add(Seance(formation_id=fo.id, day=s.day + shift, time_label=s.time_label, topic=s.topic,
                                  trainer=s.trainer))
        fo.ends_on = old[-1].day + shift
    log("formation_duplicated", f"{src.title} → {fo.title}")
    db.session.commit()
    flash(_("Nouvelle session créée (brouillon). Vérifiez les dates puis ouvrez les inscriptions."), "success")
    return redirect(url_for("formation_detail", fid=fo.id, tab="infos"))


@app.route("/formations/<int:fid>/supprimer", methods=["POST"])
@role_required("admin")
def formation_delete(fid):
    fo = Formation.query.get_or_404(fid)
    paid = Payment.query.join(Enrollment).filter(Enrollment.formation_id == fo.id, Payment.status == "paid").count()
    if paid:
        flash(_("Suppression impossible : {n} paiement(s) encaissé(s). Passez plutôt la formation en « Terminée ».", n=paid), "error")
        return redirect(url_for("formation_detail", fid=fo.id, tab="infos"))
    if request.form.get("confirm", "").strip().lower() != "supprimer":
        flash(_("Tapez SUPPRIMER pour confirmer."), "error")
        return redirect(url_for("formation_detail", fid=fo.id, tab="infos"))
    log("formation_deleted", fo.title)
    db.session.delete(fo)
    db.session.commit()
    flash(_("Formation supprimée."), "success")
    return redirect(url_for("formations"))


@app.route("/seances/<int:sid>/emargement")
@login_required
def signin_sheet(sid):
    s = Seance.query.get_or_404(sid)
    if not can_access_seance(s):
        abort(403)
    fo = s.formation
    enrs = (fo.enrollments.filter_by(status="confirmed").join(Student)
            .order_by(Student.last_name, Student.first_name).all())
    att = {a.enrollment_id: a for a in s.attendances}
    return render_template("signin_sheet.html", s=s, fo=fo, enrs=enrs, att=att,
                           blank=bool(request.args.get("vierge")))


@app.route("/formations/<int:fid>/attestations.pdf")
@role_required("manage")
def certificates_bulk(fid):
    from pdfs import certificates_pdf
    fo = Formation.query.get_or_404(fid)
    stats = attendance_stats(fo)
    items = []
    for r in stats["rows"]:
        e = r["enr"]
        if not cert_eligible(e, r)[0]:
            continue
        if not e.cert_no:
            e.cert_issued_on = today()
            db.session.flush()
            e.cert_no = f"ATT-{e.cert_issued_on.year}-{e.id:05d}"
        items.append((e.student, fo, e, r, f"{base_url()}/verifier/{e.token}"))
    if not items:
        flash(_("Aucun stagiaire ne remplit encore les conditions de l'attestation."), "error")
        return redirect(url_for("formation_detail", fid=fo.id, tab="assiduite"))
    db.session.commit()
    log("certificates_bulk", f"{fo.title} ({len(items)})")
    db.session.commit()
    return send_file(io.BytesIO(certificates_pdf(Setting.all(), items)), mimetype="application/pdf",
                     download_name=f"attestations_{fo.slug[:40]}.pdf")


# ── Full backup (admin) ───────────────────────────────────────────────────────────────────────────
@app.route("/sauvegarde.xlsx")
@role_required("admin")
def backup_xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    for model in (Setting, User, Student, Formation, Seance, Enrollment, Attendance, Payment, CashEntry, AuditLog):
        ws = wb.create_sheet(model.__tablename__[:30])
        cols = [c.name for c in model.__table__.columns if c.name not in ("password_hash",)]
        ws.append(cols)
        for cell in ws[1]:
            cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0B2545")
        for row in model.query.all():
            vals = []
            for c in cols:
                v = getattr(row, c)
                if c == "value" and isinstance(v, str) and v.startswith("data:"):
                    v = "(image)"
                vals.append(v)
            ws.append(vals)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    log("backup_downloaded")
    db.session.commit()
    return send_file(buf, download_name=f"sauvegarde_centre_{today()}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/stagiaires/export.xlsx")
@role_required("manage")
def students_export():
    rows = [[lab for _, lab in IMPORT_COLS] + ["Formations"]]
    for s in Student.query.order_by(Student.last_name, Student.first_name):
        rows.append([s.last_name, s.first_name, s.phone or "", s.email or "",
                     s.birth_date.strftime("%d/%m/%Y") if s.birth_date else "", s.birth_place or "", s.level or "",
                     s.address or "", s.parent_name or "", s.parent_phone or "", s.parent_email or "",
                     ", ".join(e.formation.title for e in s.enrollments if e.status != "cancelled")])
    return xlsx(rows, f"stagiaires_{today()}.xlsx")


# ── Finances: cash journal, expenses, teacher pay, profit ─────────────────────────────────────────
BANK_METHODS = ("ccp", "virement", "cheque")


def period_bounds(kind, y, n):
    """(start, end_exclusive) for kind month (n=1..12) | quarter (n=1..4) | year."""
    if kind == "year":
        return date(y, 1, 1), date(y + 1, 1, 1)
    if kind == "quarter":
        a = date(y, 3 * (n - 1) + 1, 1)
        return a, add_months(a, 3)
    a = date(y, n, 1)
    return a, add_months(a, 1)


def read_period():
    kind = request.args.get("p") if request.args.get("p") in ("month", "quarter", "year") else "month"
    y = to_int(request.args.get("y") or today().year, 2000, 2100)
    default_n = today().month if kind == "month" else (today().month - 1) // 3 + 1
    n = to_int(request.args.get("n") or default_n, 1, 12 if kind == "month" else 4)
    d1, d2 = period_bounds(kind, y, n)
    if kind == "year":
        prev, nxt = dict(p=kind, y=y - 1, n=1), dict(p=kind, y=y + 1, n=1)
    else:
        top = 12 if kind == "month" else 4
        prev = dict(p=kind, y=y - 1, n=top) if n == 1 else dict(p=kind, y=y, n=n - 1)
        nxt = dict(p=kind, y=y + 1, n=1) if n == top else dict(p=kind, y=y, n=n + 1)
    return {"kind": kind, "y": y, "n": n, "d1": d1, "d2": d2, "prev": prev, "next": nxt}


def period_label(per):
    if per["kind"] == "year":
        return str(per["y"])
    if per["kind"] == "quarter":
        return f"T{per['n']} {per['y']}"
    return f"{_(MONTHS[per['n'] - 1])} {per['y']}"


def mshort(m):
    from flask import g
    full = _(MONTHS[m - 1])
    return full if getattr(g, "lang", "fr") == "ar" else full[:4]


MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
          "novembre", "décembre"]


def finance_totals(d1, d2):
    """Income and expenses between d1 (incl.) and d2 (excl.)."""
    fees = (db.session.query(Formation.id, Formation.title, func.coalesce(func.sum(Payment.amount), 0))
            .join(Enrollment, Enrollment.formation_id == Formation.id).join(Payment, Payment.enrollment_id == Enrollment.id)
            .filter(Payment.status == "paid", Payment.paid_on >= d1, Payment.paid_on < d2)
            .group_by(Formation.id, Formation.title).all())
    by_formation = sorted([{"id": f, "title": t, "amount": int(a)} for f, t, a in fees], key=lambda x: -x["amount"])
    fees_total = sum(x["amount"] for x in by_formation)
    rows = (db.session.query(CashEntry.kind, CashEntry.category, func.coalesce(func.sum(CashEntry.amount), 0))
            .filter(CashEntry.on >= d1, CashEntry.on < d2).group_by(CashEntry.kind, CashEntry.category).all())
    other_in = {c: int(a) for k, c, a in rows if k == "in"}
    out = {c: int(a) for k, c, a in rows if k == "out"}
    income = fees_total + sum(other_in.values())
    expense = sum(out.values())
    profit = income - expense
    return {"fees": fees_total, "by_formation": by_formation, "other_in": other_in, "out": out,
            "income": income, "expense": expense, "profit": profit,
            "margin": round(profit * 100 / income) if income else 0}


def balances():
    """Current cash-in-hand and bank/CCP balances (opening balances from the settings)."""
    def s(q):
        return int(q.scalar() or 0)
    res = {}
    for name, methods in (("cash", ("cash", "autre")), ("bank", BANK_METHODS)):
        fees = s(db.session.query(func.sum(Payment.amount)).filter(Payment.status == "paid", Payment.method.in_(methods)))
        cin = s(db.session.query(func.sum(CashEntry.amount)).filter(CashEntry.kind == "in", CashEntry.method.in_(methods)))
        cout = s(db.session.query(func.sum(CashEntry.amount)).filter(CashEntry.kind == "out", CashEntry.method.in_(methods)))
        res[name] = to_int(Setting.get(f"{name}_opening"), -10 ** 9, 10 ** 10) + fees + cin - cout
    res["total"] = res["cash"] + res["bank"]
    return res


@app.route("/finances")
@role_required("admin")
def finances():
    per = read_period()
    tot = finance_totals(per["d1"], per["d2"])
    # previous equal period for comparison
    pk = per["prev"]
    p1, p2 = period_bounds(pk["p"], pk["y"], pk["n"])
    prev = finance_totals(p1, p2)
    # month by month over the year of the period
    y = per["y"]
    months = []
    for m in range(1, 13):
        a, b = period_bounds("month", y, m)
        t = finance_totals(a, b)
        months.append({"m": m, "label": mshort(m), "income": t["income"], "expense": t["expense"],
                       "profit": t["profit"]})
    quarters = []
    for q in range(1, 5):
        a, b = period_bounds("quarter", y, q)
        t = finance_totals(a, b)
        quarters.append({"q": q, "income": t["income"], "expense": t["expense"], "profit": t["profit"]})
    ytot = finance_totals(*period_bounds("year", y, 1))
    mmax = max([max(x["income"], x["expense"]) for x in months] + [1])
    if request.args.get("export"):
        rows = [[_("Mois"), _("Recettes"), _("Charges"), _("Bénéfice")]]
        for x in months:
            rows.append([_(MONTHS[x["m"] - 1]), x["income"], x["expense"], x["profit"]])
        rows.append([str(y), ytot["income"], ytot["expense"], ytot["profit"]])
        rows.append([])
        rows.append([_("Catégorie"), _("Montant (DA)")])
        for c, a in sorted(ytot["out"].items(), key=lambda i: -i[1]):
            rows.append([L[c], a])
        return xlsx(rows, f"finances_{y}.xlsx")
    return render_template("finances.html", per=per, tot=tot, prev=prev, months=months, quarters=quarters,
                           ytot=ytot, mmax=mmax, label=period_label(per), bal=balances())


@app.route("/caisse", methods=["GET", "POST"])
@role_required("manage")
def cashbox():
    if request.method == "POST":
        f = request.form
        if f.get("action") == "delete":
            e = CashEntry.query.get_or_404(f.get("entry_id", type=int))
            if not current_user.can("admin"):
                abort(403)
            log("cash_deleted", f"{e.category} {e.amount}")
            db.session.delete(e)
            db.session.commit()
            flash(_("Mouvement supprimé."), "success")
            return redirect(request.referrer or url_for("cashbox"))
        kind = "in" if f.get("kind") == "in" else "out"
        cat = f.get("category", "")
        if cat not in (CASH_IN if kind == "in" else CASH_OUT):
            cat = "autre_recette" if kind == "in" else "autre_charge"
        amount = to_int(f.get("amount"), 0, 100_000_000)
        if not amount:
            flash(_("Montant invalide."), "error")
            return redirect(request.referrer or url_for("cashbox"))
        m = f.get("method", "cash")
        e = CashEntry(kind=kind, category=cat, amount=amount, on=parse_date(f.get("on")) or today(),
                      method=m if m in PAY_METHODS else "cash", label=f.get("label", "").strip()[:160] or None,
                      ref=f.get("ref", "").strip()[:40] or None, created_by=current_user.name)
        db.session.add(e)
        log("cash_" + kind, f"{cat} {amount}")
        db.session.commit()
        flash(_("Mouvement enregistré."), "success")
        return redirect(request.referrer or url_for("cashbox"))
    d1 = parse_date(request.args.get("du")) or today().replace(day=1)
    d2 = parse_date(request.args.get("au")) or today()
    flt = request.args.get("f", "")
    pays = (Payment.query.filter(Payment.status == "paid", Payment.paid_on >= d1, Payment.paid_on <= d2).all()
            if flt in ("", "in", "fees") else [])
    q = CashEntry.query.filter(CashEntry.on >= d1, CashEntry.on <= d2)
    if flt == "in":
        q = q.filter(CashEntry.kind == "in")
    elif flt in ("out", "salaire"):
        q = q.filter(CashEntry.kind == "out")
        if flt == "salaire":
            q = q.filter(CashEntry.category == "salaire")
    entries = q.all() if flt != "fees" else []
    lines = []
    for p in pays:
        e = p.enrollment
        lines.append({"on": p.paid_on, "kind": "in", "cat": "fees", "label": f"{e.student.full_name} · {e.formation.title}",
                      "sub": p.label, "method": p.method, "amount": p.amount, "ref": p.receipt_no, "by": p.collected_by,
                      "pid": p.id, "eid": None})
    for c in entries:
        lab = c.label or ""
        if c.category == "salaire" and c.teacher:
            lab = f"{c.teacher.name}" + (f" · {c.period}" if c.period else "") + (f" · {lab}" if lab else "")
        lines.append({"on": c.on, "kind": c.kind, "cat": c.category, "label": lab, "sub": "", "method": c.method,
                      "amount": c.amount, "ref": c.ref, "by": c.created_by, "pid": None, "eid": c.id})
    lines.sort(key=lambda x: (x["on"], x["kind"] == "out"), reverse=True)
    tin = sum(x["amount"] for x in lines if x["kind"] == "in")
    tout = sum(x["amount"] for x in lines if x["kind"] == "out")
    by_method = {}
    for x in lines:
        sgn = 1 if x["kind"] == "in" else -1
        by_method[x["method"] or "autre"] = by_method.get(x["method"] or "autre", 0) + sgn * x["amount"]
    if request.args.get("export"):
        rows = [[_("Date"), _("Sens"), _("Catégorie"), _("Libellé"), _("Mode"), _("Montant (DA)"), _("Référence"), _("Par")]]
        for x in sorted(lines, key=lambda x: x["on"]):
            rows.append([x["on"].strftime("%d/%m/%Y"), _("Entrée") if x["kind"] == "in" else _("Sortie"),
                         L.get(x["cat"], x["cat"]), (x["label"] + (" · " + x["sub"] if x["sub"] else "")),
                         L.get(x["method"], x["method"]), x["amount"] if x["kind"] == "in" else -x["amount"],
                         x["ref"] or "", x["by"] or ""])
        return xlsx(rows, f"caisse_{d1}_{d2}.xlsx")
    return render_template("cashbox.html", lines=lines, d1=d1, d2=d2, tin=tin, tout=tout, by_method=by_method,
                           flt=flt, bal=balances(), cash_in=CASH_IN, cash_out=CASH_OUT)


def teacher_seances(u, d1, d2):
    """Séances held by a teacher between d1 (incl.) and d2 (excl.), up to today."""
    end = min(d2, today() + timedelta(days=1))
    q = (Seance.query.join(Formation).filter(Seance.day >= d1, Seance.day < end)
         .filter(or_(Seance.teacher_id == u.id, (Seance.teacher_id.is_(None)) & (Formation.trainer_id == u.id))))
    return q.order_by(Seance.day).all()


def teacher_due(u, seances):
    hours = round(sum(s.duration_minutes() for s in seances) / 60, 2)
    rate = u.pay_rate or 0
    if u.pay_mode == "hour":
        due = round(hours * rate)
    elif u.pay_mode == "seance":
        due = len(seances) * rate
    elif u.pay_mode == "month":
        due = rate if seances or rate else 0
    else:
        due = 0
    return hours, due


@app.route("/enseignants", methods=["GET", "POST"])
@role_required("admin")
def teachers():
    y = to_int(request.args.get("y") or today().year, 2000, 2100)
    m = to_int(request.args.get("m") or today().month, 1, 12)
    d1, d2 = period_bounds("month", y, m)
    period = f"{y}-{m:02d}"
    if request.method == "POST":
        f = request.form
        u = User.query.get_or_404(f.get("user_id", type=int))
        if f.get("action") == "rate":
            u.pay_mode = f.get("pay_mode") if f.get("pay_mode") in ("hour", "seance", "month") else None
            u.pay_rate = to_int(f.get("pay_rate"), 0, 10_000_000)
            u.phone = f.get("phone", "").strip()[:30] or u.phone
            db.session.commit()
            flash(_("Tarif enregistré."), "success")
        elif f.get("action") == "pay":
            amount = to_int(f.get("amount"), 0, 100_000_000)
            if amount:
                mth = f.get("method", "cash")
                db.session.add(CashEntry(kind="out", category="salaire", amount=amount, teacher_id=u.id, period=period,
                                         on=parse_date(f.get("on")) or today(), method=mth if mth in PAY_METHODS else "cash",
                                         hours=float(f.get("hours") or 0) or None, label=f.get("label", "").strip()[:160] or None,
                                         created_by=current_user.name))
                log("teacher_paid", f"{u.name} {period} {amount}")
                db.session.commit()
                flash(_("Paiement de l'enseignant enregistré."), "success")
        return redirect(url_for("teachers", y=y, m=m))
    rows = []
    for u in User.query.filter_by(role="formateur").order_by(User.name).all():
        ss = teacher_seances(u, d1, d2)
        hours, due = teacher_due(u, ss)
        paid = int(db.session.query(func.coalesce(func.sum(CashEntry.amount), 0)).filter(
            CashEntry.category == "salaire", CashEntry.teacher_id == u.id, CashEntry.period == period).scalar())
        rows.append({"u": u, "seances": ss, "n": len(ss), "hours": hours, "due": due, "paid": paid,
                     "rest": max(0, due - paid),
                     "history": CashEntry.query.filter_by(category="salaire", teacher_id=u.id)
                     .order_by(CashEntry.on.desc()).limit(6).all()})
    prev = (y - 1, 12) if m == 1 else (y, m - 1)
    nxt = (y + 1, 1) if m == 12 else (y, m + 1)
    return render_template("teachers.html", rows=rows, y=y, m=m, label=f"{_(MONTHS[m - 1])} {y}", prev=prev, nxt=nxt,
                           tot={k: sum(r[k] for r in rows) for k in ("due", "paid", "rest")})


# ── Settings & users ──────────────────────────────────────────────────────────────────────────────
@app.route("/parametres", methods=["GET", "POST"])
@role_required("admin")
def settings():
    if request.method == "POST":
        for k in Setting.DEFAULTS:
            if k != "logo":
                Setting.set(k, request.form.get(k, "").strip()[:1000])
        f = request.files.get("logo")
        if request.form.get("remove_logo"):
            Setting.set("logo", "")
        elif f and f.filename:
            import base64
            data = f.read()
            kind = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg"}
            mime = next((m for sig, m in kind.items() if data.startswith(sig)), None)
            if not mime or len(data) > 600_000:
                flash(_("Logo : image PNG ou JPG de moins de 600 Ko."), "error")
            else:
                Setting.set("logo", f"data:image/{mime};base64," + base64.b64encode(data).decode())
        log("settings_saved")
        db.session.commit()
        flash(_("Paramètres enregistrés."), "success")
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
            flash(_("Nouveau mot de passe de {name} : {pw}", name=u.name, pw=pw), "success")
        else:
            email = f.get("email", "").strip().lower()
            with unscoped():
                taken = User.query.filter_by(email=email).first()
            if not _EMAIL_RE.match(email) or taken or not f.get("name", "").strip():
                flash(_("Nom et email (unique) requis."), "error")
                return redirect(url_for("users"))
            pw = f.get("password") or secrets.token_urlsafe(8)
            u = User(name=f["name"].strip()[:120], email=email, role=f.get("role") if f.get("role") in ROLES else "secretaire")
            u.set_password(pw)
            db.session.add(u)
            flash(_("Compte créé. Mot de passe : {pw}", pw=pw), "success")
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
            flash(_("Mot de passe actuel incorrect."), "error")
        elif len(request.form.get("new", "")) < 8:
            flash(_("8 caractères minimum."), "error")
        else:
            current_user.set_password(request.form["new"])
            db.session.commit()
            flash(_("Mot de passe modifié."), "success")
        return redirect(url_for("account"))
    return render_template("account.html")


# ── Licence, transfer of data, platform (super-admin) ─────────────────────────────────────────────
PLAN_NAMES = {"trial": "Essai gratuit", "pass": "Pass+ (1 an)", "ultimate": "Ultimate (2 ans)", "lifetime": "À vie"}
KEY_MSG = {"empty": "Saisissez une clé.", "invalid": "Clé invalide. Vérifiez-la ou contactez-nous.",
           "used": "Cette clé a déjà été utilisée par une autre école.", "already": "Cette clé est déjà activée sur votre école.",
           "other_school": "Cette clé a été émise pour un autre établissement (nom différent)."}


@app.route("/abonnement", methods=["GET", "POST"])
@role_required("admin")
def licence():
    sc = g.school
    if request.method == "POST":
        ok, res = tenancy.activate_key(sc, request.form.get("key"), today())
        if ok:
            log("licence_activated", res)
            db.session.commit()
            flash(_("Licence activée : {plan}.", plan=_(PLAN_NAMES[res])), "success")
        else:
            db.session.rollback()
            flash(_(KEY_MSG[res]), "error")
        return redirect(url_for("licence"))
    return render_template("licence.html", sc=sc, plans=PLANS, names=PLAN_NAMES,
                           sales_wa=lambda plan: wa_link(SALES_PHONE, _(
                               "Bonjour, je souhaite l'offre {plan} de HaithemEduAI pour « {name} ».",
                               plan=_(PLAN_NAMES[plan]), name=sc.name)))


@app.route("/donnees/export.hedu")
@role_required("admin")
def data_export():
    blob = tenancy.export_school()
    log("data_exported")
    db.session.commit()
    return send_file(io.BytesIO(blob), mimetype="application/octet-stream",
                     download_name=f"{g.school.slug}_{today()}.hedu")


@app.route("/donnees/import", methods=["POST"])
@role_required("admin")
def data_import():
    f = request.files.get("file")
    if request.form.get("confirm", "").strip().upper() not in ("REMPLACER", "استبدال", "REPLACE") or not f:
        flash(_("Choisissez le fichier et tapez REMPLACER pour confirmer."), "error")
        return redirect(url_for("licence"))
    try:
        counts = tenancy.import_school(f.read(), g.school)
        db.session.commit()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        print("import error", e)
        flash(_("Fichier invalide : utilisez un fichier .hedu exporté depuis HaithemEduAI."), "error")
        return redirect(url_for("licence"))
    flash(_("Données importées : {n} élèves, {f} formations, {p} paiements.", n=counts.get("student", 0),
            f=counts.get("formation", 0), p=counts.get("payment", 0)), "success")
    return redirect(url_for("dashboard"))


def super_required(fn):
    @wraps(fn)
    @login_required
    def inner(*a, **kw):
        if LOCAL or not current_user.is_super:
            abort(403)
        with unscoped():
            return fn(*a, **kw)
    return inner


def _months_back(n):
    d = today().replace(day=1)
    out = []
    for _i in range(n):
        out.append(d)
        d = (d - timedelta(days=1)).replace(day=1)
    return list(reversed(out))


def school_stats(sc):
    q = lambda m: m.query.filter_by(school_id=sc.id)  # noqa: E731
    paid = db.session.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.school_id == sc.id, Payment.status == "paid").scalar()
    last = AuditLog.query.filter_by(school_id=sc.id).order_by(AuditLog.id.desc()).first()
    return {"students": q(Student).count(), "formations": q(Formation).count(), "users": q(User).count(),
            "enrollments": q(Enrollment).filter(Enrollment.status == "confirmed").count(),
            "seances": q(Seance).count(), "collected": int(paid or 0),
            "admin": q(User).filter_by(role="admin").order_by(User.id).first(),
            "last": last.created_at if last else None}


def record_sale(sc, plan, note):
    """Every plan applied by hand is kept with the keys, so licence revenue stays complete."""
    db.session.add(LicenseKey(digest=tenancy.digest(tenancy.make_short_key()), code="(manuel)", plan=plan, note=note[:200],
                              used_school_id=sc.id, used_at=datetime.utcnow()))


@app.route("/plateforme")
@super_required
def platform():
    schools = School.query.all()
    t = today()
    used = LicenseKey.query.filter(LicenseKey.used_school_id.isnot(None), LicenseKey.plan != "trial").all()
    rev = lambda keys: sum(PLANS[k.plan][1] for k in keys)  # noqa: E731
    m0 = t.replace(day=1)
    y0 = t.replace(month=1, day=1)
    months = _months_back(12)
    sign = {m: 0 for m in months}
    sales = {m: 0 for m in months}
    for sc in schools:
        k = sc.created_at.date().replace(day=1)
        if k in sign:
            sign[k] += 1
    for k in used:
        mk = k.used_at.date().replace(day=1)
        if mk in sales:
            sales[mk] += PLANS[k.plan][1]
    by_plan = {p: sum(1 for sc in schools if sc.plan == p and not sc.expired(t)) for p in PLANS}
    g_ = {"schools": len(schools), "active": sum(1 for sc in schools if sc.active and not sc.expired(t)),
          "trial": by_plan["trial"], "paid": sum(by_plan[p] for p in ("pass", "ultimate", "lifetime")),
          "expired": sum(1 for sc in schools if sc.expired(t)), "suspended": sum(1 for sc in schools if not sc.active),
          "students": Student.query.count(), "users": User.query.filter(User.school_id.isnot(None)).count(),
          "formations": Formation.query.count(), "enrollments": Enrollment.query.filter_by(status="confirmed").count(),
          "collected": int(db.session.query(func.coalesce(func.sum(Payment.amount), 0)).filter(Payment.status == "paid").scalar() or 0),
          "rev_total": rev(used), "rev_month": rev([k for k in used if k.used_at.date() >= m0]),
          "rev_year": rev([k for k in used if k.used_at.date() >= y0]),
          "conv": round(100 * sum(1 for sc in schools if sc.plan != "trial") / len(schools)) if schools else 0,
          "renew": sum(PLANS[sc.plan][1] for sc in schools if sc.plan in ("pass", "ultimate") and sc.expires_on
                       and t <= sc.expires_on <= t + timedelta(days=60))}
    soon = sorted([sc for sc in schools if sc.expires_on and -30 <= (sc.expires_on - t).days <= 30],
                  key=lambda sc: sc.expires_on)
    counts = dict(db.session.query(Student.school_id, func.count(Student.id)).group_by(Student.school_id).all())
    top = sorted(schools, key=lambda sc: -counts.get(sc.id, 0))[:8]
    recent = sorted(schools, key=lambda sc: sc.created_at, reverse=True)[:8]
    mx = max(list(sign.values()) + [1]), max(list(sales.values()) + [1])
    return render_template("platform.html", g_=g_, by_plan=by_plan, months=months, sign=sign, sales=sales, mx=mx,
                           soon=soon, top=top, counts=counts, recent=recent, pnames={k: _(v) for k, v in PLAN_NAMES.items()}, plans=PLANS, tab="home")


@app.route("/plateforme/ecoles", methods=["GET", "POST"])
@super_required
def platform_schools():
    if request.method == "POST":
        f = request.form
        err = _signup_form_errors(f)
        if err:
            flash(err, "error")
        else:
            plan = f.get("plan") if f.get("plan") in PLANS else "trial"
            sc, _u = create_school(f["centre_name"].strip(), f["name"].strip(), f["email"].strip().lower(), f["password"],
                                   phone=f.get("phone", ""), city=f.get("city", ""), plan=plan)
            if plan != "trial":
                record_sale(sc, plan, "création par la plateforme")
            db.session.commit()
            flash(_("École « {name} » créée ({plan}).", name=sc.name, plan=_(PLAN_NAMES[plan])), "success")
            return redirect(url_for("platform_school", sid=sc.id))
    t = today()
    st = request.args.get("statut", "")
    qtxt = request.args.get("q", "").strip().lower()
    items = School.query.order_by(School.created_at.desc()).all()
    def keep(sc):
        if qtxt and qtxt not in (sc.name + " " + (sc.city or "") + " " + (sc.phone or "")).lower():
            return False
        return {"": True, "essai": sc.plan == "trial" and not sc.expired(t), "payant": sc.plan != "trial" and not sc.expired(t),
                "expire": sc.expired(t), "bientot": sc.expires_on is not None and 0 <= (sc.expires_on - t).days <= 15,
                "suspendue": not sc.active}.get(st, True)
    items = [sc for sc in items if keep(sc)]
    stats = {sc.id: school_stats(sc) for sc in items}
    return render_template("platform.html", tab="schools", items=items, stats=stats, st=st, q=qtxt,
                           pnames={k: _(v) for k, v in PLAN_NAMES.items()}, plans=PLANS)


@app.route("/plateforme/ecoles/<int:sid>", methods=["GET", "POST"])
@super_required
def platform_school(sid):
    sc = School.query.get_or_404(sid)
    if request.method == "POST":
        f, act = request.form, request.form.get("action")
        if act == "info":
            sc.name = f.get("name", sc.name).strip()[:160] or sc.name
            sc.city, sc.phone = f.get("city", "").strip()[:80], f.get("phone", "").strip()[:30]
            sc.notes = f.get("notes", "")[:4000]
            sc.support_until = parse_date(f.get("support_until"))
            flash(_("Informations enregistrées."), "success")
        elif act == "plan" and f.get("plan") in PLANS:
            tenancy.apply_plan(sc, f["plan"], today())
            if f["plan"] != "trial":
                record_sale(sc, f["plan"], f.get("note") or "appliqué par la plateforme")
            flash(_("{plan} appliqué.", plan=_(PLAN_NAMES[f["plan"]])), "success")
        elif act == "expiry":
            sc.expires_on = parse_date(f.get("expires_on"))
            flash(_("Date d'expiration modifiée.") if sc.expires_on else _("Licence rendue définitive."), "success")
        elif act == "toggle":
            sc.active = not sc.active
            flash(_("École réactivée.") if sc.active else _("École suspendue : ses utilisateurs ne peuvent plus se connecter."), "success")
        elif act in ("user_reset", "user_toggle"):
            u = User.query.filter_by(id=f.get("user_id", type=int), school_id=sc.id).first_or_404()
            if act == "user_reset":
                pw = secrets.token_urlsafe(8)
                u.set_password(pw)
                flash(_("Nouveau mot de passe de {name} ({email}) : {pw}", name=u.name, email=u.email, pw=pw), "success")
            else:
                u.active = not u.active
        elif act == "delete":
            if f.get("confirm", "").strip() != sc.name:
                flash(_("Tapez le nom exact de l'école pour confirmer la suppression."), "error")
                return redirect(url_for("platform_school", sid=sc.id))
            for m in (Attendance, Payment, CashEntry, Enrollment, Seance, Formation, Student, Setting, AuditLog, User):
                m.query.filter_by(school_id=sc.id).delete(synchronize_session=False)
            name = sc.name
            db.session.delete(sc)
            db.session.commit()
            flash(_("École « {name} » et toutes ses données supprimées.", name=name), "success")
            return redirect(url_for("platform_schools"))
        db.session.commit()
        return redirect(url_for("platform_school", sid=sc.id))
    stx = school_stats(sc)
    users = User.query.filter_by(school_id=sc.id).order_by(User.role, User.name).all()
    keys = LicenseKey.query.filter_by(used_school_id=sc.id).order_by(LicenseKey.id.desc()).all()
    logs = AuditLog.query.filter_by(school_id=sc.id).order_by(AuditLog.id.desc()).limit(30).all()
    with as_school(sc.id):
        sets = Setting.all()
    return render_template("platform.html", tab="school", sc=sc, stx=stx, users=users, keys=keys, logs=logs, sets=sets,
                           pnames={k: _(v) for k, v in PLAN_NAMES.items()}, plans=PLANS, revenue=sum(PLANS[k.plan][1] for k in keys if k.plan != "trial"))


@app.route("/plateforme/cles", methods=["GET", "POST"])
@super_required
def platform_keys():
    new_key = None
    if request.method == "POST":
        f = request.form
        if f.get("action") == "revoke":
            k = LicenseKey.query.get_or_404(f.get("key_id", type=int))
            if not k.used_school_id:
                db.session.delete(k)
                flash(_("Clé supprimée."), "success")
            db.session.commit()
            return redirect(url_for("platform_keys"))
        if f.get("plan") in PLANS:
            plan, bound = f["plan"], f.get("bound_name", "").strip()
            if f.get("kind") == "signed" or plan == "lifetime":
                if not tenancy.can_sign():
                    flash(_("LICENSE_PRIVATE_KEY absente : impossible de signer une clé hors ligne."), "error")
                    return redirect(url_for("platform_keys"))
                code = tenancy.make_signed_key(plan, bound)
            else:
                code, bound = tenancy.make_short_key(), ""
            db.session.add(LicenseKey(digest=tenancy.digest(code), code=code, plan=plan, bound_name=bound or None,
                                      note=f.get("note", "")[:200]))
            db.session.commit()
            new_key = code
    st = request.args.get("statut", "")
    q = LicenseKey.query.filter(LicenseKey.code != "(manuel)")
    if st == "libre":
        q = q.filter(LicenseKey.used_school_id.is_(None))
    elif st == "utilisee":
        q = q.filter(LicenseKey.used_school_id.isnot(None))
    keys = q.order_by(LicenseKey.id.desc()).limit(200).all()
    names = {sc.id: sc.name for sc in School.query.all()}
    return render_template("platform.html", tab="keys", keys=keys, names=names, new_key=new_key, st=st,
                           pnames={k: _(v) for k, v in PLAN_NAMES.items()}, plans=PLANS, can_sign=tenancy.can_sign(),
                           schools=School.query.order_by(School.name).all())


@app.route("/plateforme/entrer/<int:sid>", methods=["POST"])
@super_required
def platform_enter(sid):
    sc = School.query.get_or_404(sid)
    session["as_school"] = sc.id
    db.session.add(AuditLog(school_id=sc.id, user=current_user.name, action="platform_view", details="consultation plateforme"))
    db.session.commit()
    flash(_("Vous consultez « {name} » en tant qu'administrateur de la plateforme.", name=sc.name), "success")
    return redirect(url_for("dashboard"))


@app.route("/plateforme/sortir")
@login_required
def platform_leave():
    session.pop("as_school", None)
    return redirect(url_for("platform"))


@app.route("/plateforme/export.xlsx")
@super_required
def platform_export():
    rows = [["École", "Ville", "Téléphone", "Offre", "Expiration", "Active", "Créée le", "Responsable", "Email",
             "Élèves", "Formations", "Utilisateurs", "Encaissé (DA)"]]
    for sc in School.query.order_by(School.created_at).all():
        x = school_stats(sc)
        rows.append([sc.name, sc.city or "", sc.phone or "", PLAN_NAMES[sc.plan], sc.expires_on.strftime("%d/%m/%Y") if sc.expires_on else "à vie",
                     "oui" if sc.active else "suspendue", sc.created_at.strftime("%d/%m/%Y"),
                     x["admin"].name if x["admin"] else "", x["admin"].email if x["admin"] else "",
                     x["students"], x["formations"], x["users"], x["collected"]])
    return xlsx(rows, f"ecoles_{today()}.xlsx")


def ensure_superadmin():
    """SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD: the platform owner's own account (no school attached)."""
    email = (os.environ.get("SUPERADMIN_EMAIL") or "").strip().lower()
    pw = os.environ.get("SUPERADMIN_PASSWORD") or ""
    if not email or len(pw) < 8:
        return
    with app.app_context(), unscoped():
        u = User.query.filter_by(email=email).first()
        if not u:
            u = User(name="Administrateur plateforme", email=email, role="admin", is_super=True, school_id=None)
            u.set_password(pw)
            db.session.add(u)
            print(f"👑 Compte plateforme créé : {email}")
        else:
            u.is_super, u.active = True, True
            if os.environ.get("SUPERADMIN_RESET"):
                u.set_password(pw)
        db.session.commit()


ensure_superadmin()


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
    if LOCAL:      # accessible from the phones / PCs on the school's Wi-Fi
        import socket
        try:
            _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _s.connect(("8.8.8.8", 80))
            print(f"📶 Sur le réseau de l'école : http://{_s.getsockname()[0]}:5000")
        except OSError:
            pass
        app.run(host="0.0.0.0", port=5000, debug=False)
    else:
        app.run(debug=True, port=5000)
