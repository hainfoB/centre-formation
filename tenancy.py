"""Multi-école : isolation automatique des données par école, licences et transfert des données.

Chaque ligne d'une table « Tenant » porte school_id. Pendant une requête, g.school_id désigne l'école
courante : toutes les requêtes ORM (SELECT, UPDATE, DELETE en masse) sont filtrées sur cette école et
chaque nouvel objet la reçoit automatiquement. Sans école courante, rien n'est visible, sauf dans un
bloc « with unscoped(): » (connexion, panneau plateforme, liens publics)."""
import base64
import hashlib
import json
import os
import secrets
import zlib
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from flask import g, has_app_context
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from models import (PLANS, Attendance, AuditLog, CashEntry, Enrollment, Formation, LicenseKey, Payment, School,
                    Seance, Setting, Student, Tenant, User, db)


# ── Isolation ─────────────────────────────────────────────────────────────────────────────────────
def current_school_id():
    return g.get("school_id") if has_app_context() else None


@contextmanager
def unscoped():
    prev = g.get("unscoped", False)
    g.unscoped = True
    try:
        yield
    finally:
        g.unscoped = prev


@contextmanager
def as_school(sid):
    prev_s, prev_u = g.get("school_id"), g.get("unscoped", False)
    g.school_id, g.unscoped = sid, False
    try:
        yield
    finally:
        g.school_id, g.unscoped = prev_s, prev_u


@event.listens_for(Session, "do_orm_execute")
def _tenant_criteria(state):
    if not has_app_context() or g.get("unscoped"):
        return
    if state.is_select and (state.is_column_load or state.is_relationship_load):
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    sid = g.get("school_id") or -1
    state.statement = state.statement.options(
        with_loader_criteria(Tenant, lambda cls: cls.school_id == sid, include_aliases=True))


@event.listens_for(Session, "before_flush")
def _tenant_stamp(session, _ctx, _inst):
    sid = current_school_id()
    for obj in session.new:
        if isinstance(obj, Tenant) and obj.school_id is None and sid:
            obj.school_id = sid


def school_slug(name):
    import re
    import unicodedata
    base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name or "").lower().encode("ascii", "ignore").decode()).strip("-")[:40] or "ecole"
    slug, i = base, 2
    with unscoped():
        while School.query.filter_by(slug=slug).first():
            slug, i = f"{base}-{i}", i + 1
    return slug


# ── Licences ──────────────────────────────────────────────────────────────────────────────────────
# Clé publique de vérification (la clé privée n'existe que sur la plateforme : LICENSE_PRIVATE_KEY).
LICENSE_PUBLIC_KEY = "bswXd3tZ2/eg/EIO6vB8NuhU6iXHJoru+lInBVfrXF4="
_PLAN_CODES = {"trial": 1, "pass": 2, "ultimate": 3, "lifetime": 4}
_CODE_PLANS = {v: k for k, v in _PLAN_CODES.items()}
_EPOCH = date(2026, 1, 1)
_ALPH = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O, 1/I


def norm_name(n):
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", n or "").lower() if ch.isalnum())


def _name_tag(n):
    return hashlib.sha256(norm_name(n).encode()).digest()[:4] if norm_name(n) else b"\0\0\0\0"


def digest(code):
    return hashlib.sha256(clean_key(code).encode()).hexdigest()


def clean_key(code):
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def can_sign():
    return bool(os.environ.get("LICENSE_PRIVATE_KEY"))


def make_short_key():
    """Online key HEDU-XXXX-XXXX-XXXX (checked against the platform database)."""
    body = "".join(secrets.choice(_ALPH) for _ in range(12))
    return "HEDU-" + "-".join(body[i:i + 4] for i in range(0, 12, 4))


def make_signed_key(plan, bound_name=""):
    """Activation code verifiable offline (local installs), signed with the platform's private key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ["LICENSE_PRIVATE_KEY"]))
    issued = (date.today() - _EPOCH).days
    payload = bytes([1, _PLAN_CODES[plan]]) + issued.to_bytes(2, "big") + secrets.token_bytes(3) + _name_tag(bound_name)
    raw = base64.b32encode(payload + priv.sign(payload)).decode().rstrip("=")
    return "HEDUL-" + "-".join(raw[i:i + 6] for i in range(0, len(raw), 6))


def read_signed_key(code):
    """→ (plan, name_tag) if the signature is valid, else None."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    c = clean_key(code)
    if not c.startswith("HEDUL"):
        return None
    c = c[5:]
    try:
        raw = base64.b32decode(c + "=" * (-len(c) % 8))
    except Exception:  # noqa: BLE001
        return None
    payload, sig = raw[:11], raw[11:]
    if len(sig) != 64 or payload[0] != 1 or payload[1] not in _CODE_PLANS:
        return None
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(LICENSE_PUBLIC_KEY)).verify(sig, payload)
    except InvalidSignature:
        return None
    return _CODE_PLANS[payload[1]], payload[7:11]


def apply_plan(school, plan, today):
    days = PLANS[plan][0]
    school.plan = plan
    if plan == "lifetime":
        school.expires_on = None
        school.support_until = today + timedelta(days=183)
    else:
        start = max(today, school.expires_on or today)
        school.expires_on = start + timedelta(days=days)


def activate_key(school, code, today):
    """Apply a licence key to a school. → (ok, message key)."""
    code = (code or "").strip()
    if not clean_key(code):
        return False, "empty"
    dg = digest(code)
    with unscoped():
        row = LicenseKey.query.filter_by(digest=dg).first()
        signed = read_signed_key(code)
        if signed:
            plan, tag = signed
            if tag != b"\0\0\0\0" and tag != _name_tag(school.name):
                return False, "other_school"
        elif row and not clean_key(code).startswith("HEDUL"):
            plan = row.plan
        else:
            return False, "invalid"
        if row and row.used_school_id and row.used_school_id != school.id:
            return False, "used"
        if row and row.used_school_id == school.id:
            return False, "already"
        if not row:      # signed key seen for the first time on this database
            row = LicenseKey(digest=dg, code=code, plan=plan)
            db.session.add(row)
        row.used_school_id, row.used_at = school.id, datetime.utcnow()
        apply_plan(school, plan, today)
    return True, plan


# ── Transfert des données d'une école (local ⇄ en ligne) ────────────────────────────────────────
_ORDER = [Student, Formation, Seance, Enrollment, Attendance, Payment, CashEntry]
_FKS = {Formation: {"trainer_id": User}, Seance: {"formation_id": Formation, "teacher_id": User},
        Enrollment: {"formation_id": Formation, "student_id": Student},
        Attendance: {"seance_id": Seance, "enrollment_id": Enrollment}, Payment: {"enrollment_id": Enrollment},
        CashEntry: {"teacher_id": User}}
_UNIQUE = {Student: ["parent_token"], Formation: ["slug"], Seance: ["scan_token", "room_token"],
           Enrollment: ["token"], Payment: ["receipt_no"]}
_USER_FIELDS = ["name", "email", "password_hash", "role", "active", "phone", "pay_mode", "pay_rate", "lang"]


def _val(v):
    if isinstance(v, datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, date):
        return {"$d": v.isoformat()}
    return v


def _unval(v):
    if isinstance(v, dict) and "$dt" in v:
        return datetime.fromisoformat(v["$dt"])
    if isinstance(v, dict) and "$d" in v:
        return date.fromisoformat(v["$d"])
    return v


def _cols(model):
    return [c.name for c in model.__table__.columns if c.name != "school_id"]


def export_school():
    """Whole data of the current school as compressed JSON bytes."""
    out = {"format": "haithemeduai-ecole", "version": 1, "exported_at": datetime.utcnow().isoformat(),
           "settings": {r.key: r.value for r in Setting.query.all()},
           "users": [{c: _val(getattr(u, c)) for c in ["id"] + _USER_FIELDS} for u in User.query.all()]}
    for m in _ORDER:
        out[m.__tablename__] = [{c: _val(getattr(r, c)) for c in _cols(m)} for r in m.query.order_by(m.id).all()]
    return zlib.compress(json.dumps(out, ensure_ascii=False).encode(), 9)


def import_school(blob, school):
    """Replace the current school's data with an export (users are merged by email). → counts."""
    try:
        data = json.loads(zlib.decompress(blob).decode())
    except Exception:  # noqa: BLE001
        data = json.loads(blob.decode())
    if data.get("format") != "haithemeduai-ecole":
        raise ValueError("format")
    for m in reversed(_ORDER):                       # wipe (scoped to this school)
        m.query.delete(synchronize_session=False)
    db.session.flush()
    idmap = {User: {}}
    for u in data.get("users", []):
        with unscoped():
            ex = User.query.filter_by(email=u["email"]).first()
        if ex and ex.school_id == school.id:
            idmap[User][u["id"]] = ex.id
        elif not ex:
            nu = User(school_id=school.id, **{c: _unval(u.get(c)) for c in _USER_FIELDS})
            db.session.add(nu)
            db.session.flush()
            idmap[User][u["id"]] = nu.id
    counts = {}
    for m in _ORDER:
        idmap[m] = {}
        rows = data.get(m.__tablename__, [])
        counts[m.__tablename__] = len(rows)
        for r in rows:
            vals = {k: _unval(v) for k, v in r.items() if k in _cols(m)}
            old_id = vals.pop("id")
            for fk, target in _FKS.get(m, {}).items():
                if vals.get(fk) is not None:
                    vals[fk] = idmap[target].get(vals[fk])
            for u in _UNIQUE.get(m, []):
                if vals.get(u):
                    with unscoped():
                        if m.query.filter(getattr(m, u) == vals[u]).first():
                            vals[u] = (vals[u] + "-" + secrets.token_hex(2)) if u in ("slug", "receipt_no") \
                                else secrets.token_urlsafe(24)
            obj = m(school_id=school.id, **vals)
            db.session.add(obj)
            db.session.flush()
            idmap[m][old_id] = obj.id
    for k, v in (data.get("settings") or {}).items():
        Setting.set(k, v)
    db.session.add(AuditLog(school_id=school.id, user="import", action="import_donnees",
                            details=json.dumps(counts)[:300]))
    return counts


# ── Mise en place / migration de l'ancienne base mono-école ────────────────────────────────────
def bootstrap(engine, today):
    """Called at startup: old single-school database → school n°1 (all rows tagged, owner = super)."""
    from sqlalchemy import inspect as sa_inspect, text
    with unscoped():
        insp = sa_inspect(engine)
        n_users = db.session.execute(text('SELECT COUNT(*) FROM "user"')).scalar()
        if School.query.count() or not n_users:
            return
        old = {}
        if insp.has_table("setting"):
            old = dict(db.session.execute(text('SELECT "key", value FROM setting')).all())
        name = old.get("centre_name") or "Mon école"
        s = School(name=name, slug=school_slug(name), plan="lifetime", expires_on=None,
                   support_until=today + timedelta(days=183))
        db.session.add(s)
        db.session.flush()
        for m in [Setting, User, Student, Formation, Seance, Enrollment, Attendance, Payment, CashEntry, AuditLog]:
            db.session.execute(text(f'UPDATE "{m.__tablename__}" SET school_id = :s WHERE school_id IS NULL'), {"s": s.id})
        for k, v in old.items():
            db.session.add(Setting(school_id=s.id, key=k, value=v))
        first = User.query.filter_by(role="admin").order_by(User.id).first()
        if first:
            first.is_super = True
        db.session.commit()
        print(f"🏫 Base existante migrée vers l'école n°{s.id} « {name} »")
