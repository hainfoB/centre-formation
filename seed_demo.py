"""Fill a LOCAL database with realistic demo data (fictional school, students, payments, expenses).

    python seed_demo.py            → (re)creates the demo data in the local SQLite database
Never run it against the production database: it refuses when DATABASE_URL points to PostgreSQL
unless --force is given."""
import os
import random
import sys
from datetime import date, datetime, timedelta

if os.environ.get("DATABASE_URL", "").startswith(("postgres", "postgresql")) and "--force" not in sys.argv:
    sys.exit("Refusé : DATABASE_URL pointe vers PostgreSQL (production). Utilisez la base locale.")

os.environ.setdefault("DISABLE_SCHEDULER", "1")
from app import add_months, app, today  # noqa: E402
from models import (Attendance, AuditLog, CashEntry, Enrollment, Formation, Payment, Seance, Setting, Student,  # noqa: E402
                    User, db)

random.seed(42)
T = today()

FIRST_M = ["Amine", "Yacine", "Rayan", "Adam", "Mohamed", "Ilyes", "Anis", "Walid", "Karim", "Sofiane", "Nassim", "Islam",
           "Abdelkader", "Riad", "Oussama", "Haroun", "Mehdi", "Zakaria"]
FIRST_F = ["Lina", "Sara", "Meriem", "Amel", "Yasmine", "Nour", "Ines", "Rania", "Aya", "Imane", "Kenza", "Malak", "Lyna",
           "Assia", "Chaima", "Hiba", "Sirine", "Douaa"]
LAST = ["Benali", "Kaci", "Zerrouk", "Boudiaf", "Hamidi", "Mansouri", "Belkacem", "Saidi", "Cherif", "Ouali", "Brahimi",
        "Meziane", "Haddad", "Djebbar", "Bouzid", "Rahmani", "Taleb", "Amrani", "Lounis", "Ferhat", "Khelifi", "Bensalem"]
PARENT_M = ["Karim", "Rachid", "Farid", "Mourad", "Nabil", "Djamel", "Samir", "Hakim", "Omar", "Mustapha"]


def phone():
    return random.choice(["05", "06", "07"]) + "".join(random.choice("0123456789") for _ in range(8))


def make_student(child=True):
    female = random.random() < .5
    first = random.choice(FIRST_F if female else FIRST_M)
    last = random.choice(LAST)
    s = Student(first_name=first, last_name=last, phone=phone() if not child or random.random() < .5 else None,
                level=None, address=random.choice(["Cité 500 logements", "Cité El Nasr", "Centre-ville", "Cité 1000 logements",
                                                   "Hai El Djorf", "Cité Djaafria"]) + ", Bordj Bou Arréridj")
    if child:
        s.parent_name = f"{random.choice(PARENT_M)} {last}"
        s.parent_phone = phone()
        s.birth_date = date(T.year - random.randint(4, 16), random.randint(1, 12), random.randint(1, 28))
    else:
        s.email = f"{first.lower()}.{last.lower()}{random.randint(1, 99)}@gmail.com"
        s.birth_date = date(T.year - random.randint(19, 40), random.randint(1, 12), random.randint(1, 28))
        s.level = random.choice(["Licence", "Master", "Terminale", "BTS"])
    s.birth_place = "Bordj Bou Arréridj"
    db.session.add(s)
    return s


def seances(fo, start, end, weekdays, t1, t2):
    d, out = start, []
    while d <= end:
        if d.weekday() in weekdays:
            out.append(Seance(formation_id=fo.id, day=d, time_label=f"{t1} - {t2}"))
        d += timedelta(days=1)
    db.session.add_all(out)
    return out


def schedule(enr, fo, start, paid_ratio=1.0, late_chance=.15):
    """Instalments from the formation start, paid when due date is past (with some late payers)."""
    n = 0
    items = []
    if fo.registration_fee:
        items.append((0, "Frais d'inscription", start, fo.registration_fee))
    k = fo.installments or 1
    base, rest = divmod(max(0, fo.price_total - (enr.discount or 0)), k)
    for i in range(k):
        items.append((i + 1, "Paiement unique" if k == 1 else f"Mensualité {i + 1}/{k}", add_months(start, i),
                      base + (rest if i == 0 else 0)))
    bad_payer = random.random() < late_chance
    for num, label, due, amount in items:
        p = Payment(enrollment_id=enr.id, number=num, label=label, due_on=due, amount=amount)
        if due <= T and (not bad_payer or due < T - timedelta(days=45)) and random.random() < paid_ratio:
            p.status = "paid"
            p.paid_on = min(T, due + timedelta(days=random.randint(0, 6)))
            p.method = random.choice(["cash", "cash", "cash", "ccp", "virement"])
            p.collected_by = "Secrétariat"
        db.session.add(p)
        n += 1
    db.session.flush()
    for p in Payment.query.filter_by(enrollment_id=enr.id, status="paid"):
        p.receipt_no = f"R{p.paid_on.year}-{p.id:05d}"
    return n


def attendance(fo, enrs, absentees=()):
    for s in fo.seances.filter(Seance.day <= T).all():
        a, b = s.bounds()
        for e in enrs:
            r = random.random()
            if e.id in absentees and s.day > T - timedelta(days=12):
                st = "absent"
            else:
                st = "present" if r < .82 else "late" if r < .9 else "excused" if r < .94 else "absent"
            att = Attendance(seance_id=s.id, enrollment_id=e.id, status=st)
            if st in ("present", "late"):
                mins = a - random.randint(2, 12) if st == "present" else a + random.randint(17, 40)
                att.checkin_at = datetime(s.day.year, s.day.month, s.day.day) + timedelta(minutes=mins - 60)
                att.method = random.choice(["ticket", "ticket", "room", "manual"])
            db.session.add(att)


def run():
    with app.app_context():
        db.drop_all()
        db.create_all()
        from flask import g
        from models import School
        import tenancy
        sc = School(name="Académie Al-Nour", slug="academie-al-nour", city="Bordj Bou Arréridj", phone="0796302802")
        tenancy.apply_plan(sc, "lifetime" if "--essai" not in sys.argv else "trial", today())
        db.session.add(sc)
        db.session.flush()
        g.school_id = sc.id
        S = {"centre_name": "Académie Al-Nour", "centre_tagline": "École privée · Crèche · Centre de formation",
             "centre_address": "12 rue de l'Indépendance, Bordj Bou Arréridj", "centre_phone": "035 00 00 00",
             "centre_email": "contact@alnour-demo.dz", "centre_agrement": "DEMO-034/2024", "director_name": "M. Directeur Démo",
             "payment_info": "Paiement en espèces au secrétariat (8h–16h) ou versement CCP n° [VOTRE CCP].",
             "cash_opening": "150000", "bank_opening": "320000", "default_lang": "fr"}
        for k, v in S.items():
            Setting.set(k, v)
        admin = User(name="Directeur Démo", email="admin@demo.dz", role="admin", is_super=True)
        sec = User(name="Samira (secrétariat)", email="secretariat@demo.dz", role="secretaire")
        t1 = User(name="Mme Amina Belhadj", email="amina@demo.dz", role="formateur", pay_mode="hour", pay_rate=1200, phone=phone())
        t2 = User(name="M. Sofiane Rahmani", email="sofiane@demo.dz", role="formateur", pay_mode="seance", pay_rate=2500, phone=phone())
        t3 = User(name="Mme Nadia Kaci", email="nadia@demo.dz", role="formateur", pay_mode="month", pay_rate=38000, phone=phone())
        t4 = User(name="M. Karim Ouali", email="karim@demo.dz", role="formateur", pay_mode="hour", pay_rate=1500, phone=phone())
        for u in (admin, sec, t1, t2, t3, t4):
            u.set_password("demo1234")
            db.session.add(u)
        db.session.flush()

        y0 = T.year if T.month >= 9 else T.year - 1
        sept = date(y0, 9, 14)
        F = []
        # 1. School support — maths
        f1 = Formation(title="Soutien scolaire — Mathématiques 4AM (BEM)", slug="soutien-maths-4am", status="running",
                       description="Cours de soutien en mathématiques pour les élèves de 4e année moyenne, préparation au BEM.",
                       programme="Calcul littéral\nÉquations et inéquations\nThéorème de Thalès\nStatistiques\nSujets type BEM",
                       audience="Élèves de 4AM", trainer=t1.name, trainer_id=t1.id, location="Salle 2", mode="onsite",
                       starts_on=sept, ends_on=date(y0 + 1, 6, 10), start_time="17:00", end_time="18:30", hours_total=96, seats=20,
                       registration_fee=1000, price_total=27000, installments=9, min_attendance=75, late_tolerance=10,
                       cert_requires_payment=True, check_mode="in")
        # 2. Crèche
        f2 = Formation(title="Crèche — Section moyenne (4–5 ans)", slug="creche-section-moyenne", status="running",
                       description="Accueil éducatif du dimanche au jeudi : éveil, langage, motricité, activités manuelles.",
                       audience="Enfants de 4 à 5 ans", trainer=t3.name, trainer_id=t3.id, location="Espace crèche", mode="onsite",
                       starts_on=sept, ends_on=date(y0 + 1, 6, 30), start_time="08:00", end_time="12:00", seats=18,
                       registration_fee=3000, price_total=90000, installments=10, min_attendance=0, late_tolerance=20,
                       cert_requires_payment=False, check_mode="inout")
        # 3. Office skills
        f3 = Formation(title="Bureautique et Excel professionnel", slug="bureautique-excel", status="running",
                       description="Word, Excel (formules, tableaux croisés dynamiques), PowerPoint. Attestation en fin de formation.",
                       programme="Word : mise en page et publipostage\nExcel : formules et fonctions\nTableaux croisés dynamiques\nPowerPoint\nProjet final",
                       audience="Adultes, demandeurs d'emploi, employés", trainer=t4.name, trainer_id=t4.id, location="Salle informatique",
                       mode="onsite", starts_on=T - timedelta(days=35), ends_on=T + timedelta(days=55), start_time="09:00",
                       end_time="12:00", hours_total=36, seats=14, registration_fee=2000, price_total=18000, installments=3,
                       min_attendance=80, late_tolerance=15, cert_requires_payment=True, check_mode="inout")
        # 4. English — registration open
        f4 = Formation(title="Anglais — Niveau B1 (adultes)", slug="anglais-b1", status="open",
                       description="Communication orale et écrite, préparation aux échanges professionnels.",
                       programme="Grammaire B1\nExpression orale\nCompréhension\nRédaction d'e-mails", audience="Adultes niveau A2",
                       trainer=t2.name, trainer_id=t2.id, location="Salle 1", mode="hybrid", online_url="https://meet.google.com/demo-demo-demo",
                       starts_on=T + timedelta(days=12), ends_on=T + timedelta(days=100), start_time="18:00", end_time="19:30",
                       hours_total=36, seats=16, registration_fee=0, price_total=12000, installments=2, min_attendance=80,
                       late_tolerance=15, auto_confirm=True, cert_requires_payment=True)
        # 5. Free workshop, done
        f5 = Formation(title="Atelier gratuit : l'IA au service des enseignants", slug="atelier-ia-enseignants", status="done",
                       is_free=True, description="Atelier de découverte : préparer ses cours avec l'IA.", trainer=t2.name,
                       trainer_id=t2.id, mode="online", online_url="https://meet.google.com/demo-ia", starts_on=T - timedelta(days=40),
                       ends_on=T - timedelta(days=26), start_time="20:00", end_time="22:00", hours_total=6, seats=30,
                       min_attendance=60, check_mode="in")
        # 6. Last school year (fills the yearly charts)
        f6 = Formation(title="Préparation BAC 2026 — Physique", slug="prepa-bac-physique-2026", status="done",
                       trainer=t2.name, trainer_id=t2.id, location="Salle 3", mode="onsite", starts_on=date(y0, 1, 10),
                       ends_on=date(y0, 6, 5), start_time="14:00", end_time="16:00", hours_total=80, seats=20,
                       registration_fee=1000, price_total=20000, installments=5, min_attendance=70)
        # 7-8. Last school year's crèche and support classes (fill the yearly accounts)
        f7 = Formation(title="Crèche — Section moyenne 2025-2026", slug="creche-2025-2026", status="done", trainer=t3.name,
                       trainer_id=t3.id, location="Espace crèche", mode="onsite", starts_on=date(y0 - 1, 9, 14),
                       ends_on=date(y0, 6, 30), start_time="08:00", end_time="12:00", seats=18, registration_fee=3000,
                       price_total=85000, installments=10, min_attendance=0, cert_requires_payment=False)
        f8 = Formation(title="Soutien scolaire — Maths et physique 2025-2026", slug="soutien-2025-2026", status="done",
                       trainer=t1.name, trainer_id=t1.id, location="Salle 2", mode="onsite", starts_on=date(y0 - 1, 9, 14),
                       ends_on=date(y0, 6, 10), start_time="17:00", end_time="18:30", seats=24, registration_fee=1000,
                       price_total=31500, installments=9, min_attendance=75)
        for f in (f1, f2, f3, f4, f5, f6, f7, f8):
            db.session.add(f)
            F.append(f)
        db.session.flush()

        seances(f1, max(sept, T - timedelta(days=70)), T + timedelta(days=28), (1, 3), "17:00", "18:30")
        seances(f2, max(sept, T - timedelta(days=42)), T + timedelta(days=21), (6, 0, 1, 2, 3), "08:00", "12:00")
        seances(f3, f3.starts_on, f3.ends_on, (5,), "09:00", "12:00")
        seances(f4, f4.starts_on, f4.ends_on, (0, 2), "18:00", "19:30")
        seances(f5, f5.starts_on, f5.ends_on, (2,), "20:00", "22:00")
        seances(f6, date(y0, 3, 1), date(y0, 5, 30), (5,), "14:00", "16:00")
        db.session.flush()
        for s in f1.seances.filter(Seance.day <= T).limit(3):
            s.topic = random.choice(["Calcul littéral", "Équations", "Thalès"])

        def enrol(fo, n, child, start, paid_ratio=1.0, late=.15, discount_chance=.1, status="confirmed"):
            out = []
            for _ in range(n):
                st = make_student(child)
                if fo is f1:
                    st.level = "4AM"
                elif fo is f2:
                    st.level = "Crèche — section moyenne"
                db.session.flush()
                e = Enrollment(formation_id=fo.id, student_id=st.id, status=status,
                               created_at=datetime.combine(start - timedelta(days=random.randint(1, 20)), datetime.min.time()),
                               discount=(random.choice([2000, 5000]) if random.random() < discount_chance and fo.price_total else 0),
                               source=random.choice(["facebook", "whatsapp", "secretariat", "web"]))
                db.session.add(e)
                db.session.flush()
                if status == "confirmed" and not fo.is_free:
                    schedule(e, fo, start, paid_ratio, late)
                out.append(e)
            return out

        e1 = enrol(f1, 16, True, sept)
        e2 = enrol(f2, 14, True, sept, discount_chance=.25)
        e3 = enrol(f3, 11, False, f3.starts_on, late=.2)
        e4 = enrol(f4, 6, False, f4.starts_on)
        enrol(f4, 3, False, f4.starts_on, status="pending")
        e5 = enrol(f5, 22, False, f5.starts_on)
        e6 = enrol(f6, 15, False, date(y0, 1, 10), late=.05)
        enrol(f7, 16, True, date(y0 - 1, 9, 14), late=.04)
        enrol(f8, 22, True, date(y0 - 1, 9, 14), late=.04)
        db.session.flush()
        attendance(f1, e1, absentees={e1[3].id})
        attendance(f2, e2, absentees={e2[5].id})
        attendance(f3, e3, absentees={e3[2].id})
        attendance(f5, e5)
        attendance(f6, e6)
        # certificates already issued for the free workshop
        for e in e5[:15]:
            e.cert_issued_on = f5.ends_on + timedelta(days=2)
        db.session.flush()
        for e in e5[:15]:
            e.cert_no = f"ATT-{e.cert_issued_on.year}-{e.id:05d}"

        # expenses over the last 12 months
        m0 = T.replace(day=1)
        for k in range(11, -1, -1):
            a = add_months(m0, -k)
            last_day = min(T, add_months(a, 1) - timedelta(days=1))
            school = a.month in (9, 10, 11, 12, 1, 2, 3, 4, 5, 6)

            def out(cat, amount, day, label=None, method="cash", ref=None):
                d = a.replace(day=min(day, 28))
                if d <= T:
                    db.session.add(CashEntry(kind="out", category=cat, amount=amount, on=d, method=method, label=label,
                                             ref=ref, created_by="Directeur Démo"))
            out("loyer", 60000, 5, "Loyer du local", "virement", f"VIR-{a:%m%y}")
            out("energie", random.randint(7, 14) * 1000 + random.choice([0, 500]), 18, "Facture Sonelgaz / ADE", "ccp")
            out("telecom", 3500, 10, "Abonnement fibre + téléphone", "ccp")
            out("entretien", 18000, 28, "Agent d'entretien")
            if school:
                out("fournitures", random.randint(4, 12) * 1000, 12, random.choice(["Rames de papier, feutres", "Cartouches d'encre", "Matériel crèche"]))
            if a.month in (8, 9):
                out("publicite", 15000, 20, "Sponsoring Facebook — rentrée")
            if a.month in (1, 4, 7, 10):
                out("impots", 25000, 20, "G50 et CNAS du trimestre", "ccp")
            if a.month == 8:
                out("equipement", 95000, 25, "2 ordinateurs pour la salle informatique", "virement")
            if school and a.month in (9, 10):
                db.session.add(CashEntry(kind="in", category="vente", amount=random.randint(18, 30) * 1000,
                                         on=a.replace(day=15) if a.replace(day=15) <= T else T, method="cash",
                                         label="Vente de livres et tabliers", created_by="Samira (secrétariat)"))
            # salaries for past months (paid at month end or start of next)
            if a < m0:
                for u, amount in ((t1, random.randint(14, 20) * 1000 if school else 0), (t2, random.randint(10, 18) * 1000 if school or a.month == 7 else 0),
                                  (t3, 38000 if school else 0), (t4, random.randint(8, 14) * 1000 if k <= 2 else 0)):
                    if amount:
                        db.session.add(CashEntry(kind="out", category="salaire", amount=amount, teacher_id=u.id,
                                                 period=f"{a:%Y-%m}", on=last_day, method=random.choice(["cash", "ccp"]),
                                                 created_by="Directeur Démo"))
        db.session.add(CashEntry(kind="in", category="apport", amount=100000, on=add_months(m0, -11).replace(day=2),
                                 method="virement", label="Apport du gérant — trésorerie de départ", created_by="Directeur Démo"))
        db.session.add(AuditLog(user="system", action="demo_seed", details="Données de démonstration"))
        db.session.commit()
        print("✅ Base de démonstration créée.")
        print(f"   Formations : {Formation.query.count()} · Stagiaires : {Student.query.count()} · "
              f"Séances : {Seance.query.count()} · Paiements : {Payment.query.count()} · Mouvements de caisse : {CashEntry.query.count()}")
        print("   Comptes : admin@demo.dz / demo1234 (direction) · secretariat@demo.dz / demo1234 · amina@demo.dz / demo1234 (formatrice)")


if __name__ == "__main__":
    run()
