"""Data model of the training centre: staff, students (with parent contact), formations, séances,
enrollments, attendance and instalment payments."""
import re
import secrets
from datetime import datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

ROLES = ("admin", "secretaire", "formateur")
FORMATION_STATUSES = ("draft", "open", "running", "done")
ENROLL_STATUSES = ("pending", "confirmed", "waitlist", "cancelled")
PAY_METHODS = ("cash", "ccp", "virement", "cheque", "autre")


def _tok(n=24):
    return secrets.token_urlsafe(n)


class Setting(db.Model):
    """Key/value settings of the centre (name, address, phones, payment instructions…)."""
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=True)

    DEFAULTS = {
        "centre_name": "Mon Centre de Formation",
        "centre_tagline": "Centre de formation professionnelle agréé",
        "centre_address": "",
        "centre_phone": "",
        "centre_email": "",
        "centre_agrement": "",
        "director_name": "",
        "payment_info": "Paiement en espèces au secrétariat ou par versement CCP.",
    }

    @classmethod
    def get(cls, key):
        row = db.session.get(cls, key)
        return row.value if row and row.value is not None else cls.DEFAULTS.get(key, "")

    @classmethod
    def set(cls, key, value):
        row = db.session.get(cls, key)
        if not row:
            row = cls(key=key)
            db.session.add(row)
        row.value = value

    @classmethod
    def all(cls):
        return {k: cls.get(k) for k in cls.DEFAULTS}


class User(UserMixin, db.Model):
    """Staff account."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(12), default="secretaire")
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_active(self):
        return bool(self.active)

    def can(self, what):
        if self.role == "admin":
            return True
        if self.role == "secretaire":
            return what != "admin"
        return what == "attendance"   # formateur


class Student(db.Model):
    """A trainee (stagiaire), with an optional parent / guardian contact."""
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(150), nullable=True, index=True)
    phone = db.Column(db.String(30), nullable=True, index=True)
    birth_date = db.Column(db.Date, nullable=True)
    birth_place = db.Column(db.String(100), nullable=True)
    address = db.Column(db.String(200), nullable=True)
    level = db.Column(db.String(100), nullable=True)          # niveau d'études
    parent_name = db.Column(db.String(120), nullable=True)
    parent_phone = db.Column(db.String(30), nullable=True)
    parent_email = db.Column(db.String(150), nullable=True)
    parent_token = db.Column(db.String(40), unique=True, nullable=False, default=_tok)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    enrollments = db.relationship("Enrollment", backref="student", lazy="dynamic",
                                  cascade="all, delete-orphan", order_by="Enrollment.created_at.desc()")

    @property
    def full_name(self):
        return f"{self.last_name.upper()} {self.first_name}".strip()

    @staticmethod
    def norm_phone(p):
        d = re.sub(r"\D", "", p or "")
        if d.startswith("213"):
            d = "0" + d[3:]
        return d


class Formation(db.Model):
    """A training course (free or paid) with its public sign-up form."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    programme = db.Column(db.Text, nullable=True)            # one line per point
    prerequisites = db.Column(db.Text, nullable=True)
    audience = db.Column(db.String(200), nullable=True)
    trainer = db.Column(db.String(120), nullable=True)
    trainer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    location = db.Column(db.String(200), nullable=True)
    mode = db.Column(db.String(10), default="onsite")        # onsite | online | hybrid
    online_url = db.Column(db.String(300), nullable=True)
    group_url = db.Column(db.String(300), nullable=True)
    starts_on = db.Column(db.Date, nullable=True)
    ends_on = db.Column(db.Date, nullable=True)
    start_time = db.Column(db.String(5), nullable=True)
    end_time = db.Column(db.String(5), nullable=True)
    hours_total = db.Column(db.Integer, default=0)
    seats = db.Column(db.Integer, default=0)                 # 0 = unlimited
    is_free = db.Column(db.Boolean, default=False)
    registration_fee = db.Column(db.Integer, default=0)      # frais d'inscription (DA), first instalment
    price_total = db.Column(db.Integer, default=0)           # tuition (DA), split into instalments
    installments = db.Column(db.Integer, default=1)
    payment_info = db.Column(db.Text, nullable=True)
    check_mode = db.Column(db.String(5), default="in")       # in | inout
    late_tolerance = db.Column(db.Integer, default=15)       # minutes
    min_attendance = db.Column(db.Integer, default=80)       # % for the certificate
    cert_requires_payment = db.Column(db.Boolean, default=True)
    auto_confirm = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(10), default="draft")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    seances = db.relationship("Seance", backref="formation", lazy="dynamic",
                              order_by="Seance.day", cascade="all, delete-orphan")
    enrollments = db.relationship("Enrollment", backref="formation", lazy="dynamic",
                                  order_by="Enrollment.created_at", cascade="all, delete-orphan")
    trainer_user = db.relationship("User")

    def taken(self):
        return self.enrollments.filter(Enrollment.status.in_(("confirmed", "pending"))).count()

    def seats_left(self):
        return None if not self.seats else max(0, self.seats - self.taken())

    def default_time_label(self):
        if self.start_time and self.end_time:
            return f"{self.start_time} - {self.end_time}"
        return self.start_time or ""

    @property
    def total_cost(self):
        return 0 if self.is_free else (self.registration_fee or 0) + (self.price_total or 0)

    @staticmethod
    def _items(text):
        return [l.strip(" -•\t") for l in (text or "").splitlines() if l.strip(" -•\t")]

    def programme_items(self):
        return self._items(self.programme)

    def prerequisite_items(self):
        return self._items(self.prerequisites)


class Seance(db.Model):
    """One class meeting (date + time), with its scan tokens and online codes."""
    id = db.Column(db.Integer, primary_key=True)
    formation_id = db.Column(db.Integer, db.ForeignKey("formation.id"), nullable=False, index=True)
    day = db.Column(db.Date, nullable=False)
    time_label = db.Column(db.String(30), nullable=True)
    topic = db.Column(db.String(200), nullable=True)
    trainer = db.Column(db.String(120), nullable=True)
    reminder_sent = db.Column(db.Boolean, default=False)
    scan_token = db.Column(db.String(40), unique=True, nullable=True)
    room_token = db.Column(db.String(40), unique=True, nullable=True)
    code_in = db.Column(db.String(4), nullable=True)
    code_out = db.Column(db.String(4), nullable=True)
    attendances = db.relationship("Attendance", backref="seance", lazy="dynamic", cascade="all, delete-orphan")

    def bounds(self):
        """(start, end) in minutes from the séance's own time or the formation's default; None if unknown."""
        for label in (self.time_label, self.formation.default_time_label()):
            mm = re.match(r"^\s*(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})\s*$", label or "")
            if mm:
                a, b = int(mm[1]) * 60 + int(mm[2]), int(mm[3]) * 60 + int(mm[4])
                if b > a:
                    return a, b
        return None

    def duration_minutes(self):
        b = self.bounds()
        return b[1] - b[0] if b else 0


class Enrollment(db.Model):
    """A student registered in a formation."""
    id = db.Column(db.Integer, primary_key=True)
    formation_id = db.Column(db.Integer, db.ForeignKey("formation.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    status = db.Column(db.String(10), default="pending", index=True)
    token = db.Column(db.String(40), unique=True, nullable=False, default=_tok)
    discount = db.Column(db.Integer, default=0)              # remise (DA) on tuition
    source = db.Column(db.String(40), nullable=True)
    cert_no = db.Column(db.String(30), nullable=True)
    cert_issued_on = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    attendances = db.relationship("Attendance", backref="enrollment", lazy="dynamic", cascade="all, delete-orphan")
    payments = db.relationship("Payment", backref="enrollment", lazy="dynamic",
                               cascade="all, delete-orphan", order_by="Payment.number")
    __table_args__ = (db.UniqueConstraint("formation_id", "student_id", name="uq_enrollment"),)


class Attendance(db.Model):
    """status: present | late | absent | excused. method: ticket | room | code | manual."""
    id = db.Column(db.Integer, primary_key=True)
    seance_id = db.Column(db.Integer, db.ForeignKey("seance.id"), nullable=False, index=True)
    enrollment_id = db.Column(db.Integer, db.ForeignKey("enrollment.id"), nullable=False, index=True)
    status = db.Column(db.String(10), nullable=True)
    checkin_at = db.Column(db.DateTime, nullable=True)       # UTC
    checkout_at = db.Column(db.DateTime, nullable=True)      # UTC
    method = db.Column(db.String(10), nullable=True)
    __table_args__ = (db.UniqueConstraint("seance_id", "enrollment_id", name="uq_attendance"),)


class Payment(db.Model):
    """One instalment. number 0 = registration fee. status: due | paid | waived."""
    id = db.Column(db.Integer, primary_key=True)
    enrollment_id = db.Column(db.Integer, db.ForeignKey("enrollment.id"), nullable=False, index=True)
    number = db.Column(db.Integer, nullable=False)
    label = db.Column(db.String(60), nullable=True)
    due_on = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(8), default="due", index=True)
    paid_on = db.Column(db.Date, nullable=True)
    method = db.Column(db.String(10), nullable=True)
    receipt_no = db.Column(db.String(30), nullable=True, unique=True)
    collected_by = db.Column(db.String(120), nullable=True)
    reminders = db.Column(db.String(40), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("enrollment_id", "number", name="uq_payment"),)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(120), nullable=True)
    action = db.Column(db.String(60), nullable=False)
    details = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
