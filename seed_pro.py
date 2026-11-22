"""École de démonstration AcadémieProLearning : formations individuelles (soutien, langues, bureautique)
ET formations pour entreprises (soudage, Lean Six Sigma, 5S, CPHS, HSE, habilitation électrique…),
deux groupes industriels fictifs avec leurs structures, salariés, comptes RH et émargements.

N'efface rien : crée une nouvelle école « institut-prolearning-demo » si elle n'existe pas.
    python seed_pro.py              (base locale)
    SEED_PRO_DEMO=1 au démarrage    (production : appelé par app.py, une seule fois)
Toutes les entreprises, personnes et adresses sont fictives."""
import random
from datetime import date, datetime, timedelta

from models import (AuditLog, Attendance, CashEntry, Company, Enrollment, Formation, Payment, School, Seance, Setting,
                    Student, User, db)

SLUG = "institut-prolearning-demo"
PW = "demo1234"

FIRST_M = ["Amine", "Yacine", "Rayan", "Mohamed", "Ilyes", "Anis", "Walid", "Karim", "Sofiane", "Nassim", "Islam",
           "Abdelkader", "Riad", "Oussama", "Mehdi", "Zakaria", "Bilal", "Hamza", "Lotfi", "Fayçal", "Mourad", "Adel"]
FIRST_F = ["Lina", "Sara", "Meriem", "Amel", "Yasmine", "Nour", "Ines", "Rania", "Imane", "Kenza", "Assia", "Chaima",
           "Hiba", "Sirine", "Nesrine", "Soumia", "Wafa", "Fatima Zohra"]
LAST = ["Benali", "Kaci", "Zerrouk", "Boudiaf", "Hamidi", "Mansouri", "Belkacem", "Saidi", "Cherif", "Ouali", "Brahimi",
        "Meziane", "Haddad", "Djebbar", "Bouzid", "Rahmani", "Taleb", "Amrani", "Lounis", "Ferhat", "Khelifi", "Bensalem",
        "Boukhalfa", "Medjahed", "Guettaf", "Achour", "Benmoussa", "Slimani", "Zitouni", "Hadjadj"]


def run(today_):  # noqa: C901
    """Create the demo school (caller sets nothing; returns the School or None if it already exists)."""
    from flask import g
    import tenancy
    from tenancy import unscoped
    with unscoped():
        if School.query.filter_by(slug=SLUG).first():
            return None
    rnd = random.Random(2026)
    T = today_

    def phone():
        return rnd.choice(["05", "06", "07"]) + "".join(rnd.choice("0123456789") for _ in range(8))

    def add_months(d, k):
        m = d.month - 1 + k
        y, m = d.year + m // 12, m % 12 + 1
        return date(y, m, min(d.day, 28))

    sc = School(name="Institut ProLearning (démo)", slug=SLUG, city="Bordj Bou Arréridj", phone="+213699102523")
    tenancy.apply_plan(sc, "lifetime", T)
    db.session.add(sc)
    db.session.flush()
    prev = g.get("school_id"), g.get("unscoped", False)
    g.school_id, g.unscoped = sc.id, False
    try:
        for k, v in {"centre_name": "Institut ProLearning", "centre_tagline": "Formation professionnelle · Entreprises · Soutien scolaire",
                     "centre_address": "Zone d'activités, Bordj Bou Arréridj", "centre_phone": "035 00 00 00",
                     "centre_email": "contact@prolearning-demo.dz", "centre_agrement": "DEMO-PRO/2026",
                     "director_name": "M. Directeur Démo", "cash_opening": "250000", "bank_opening": "900000",
                     "payment_info": "Particuliers : espèces ou CCP au secrétariat. Entreprises : virement sur convention / bon de commande.",
                     "default_lang": "fr"}.items():
            Setting.set(k, v)

        # ── Staff ────────────────────────────────────────────────────────────────────────────────
        def user(name, email, role, **kw):
            u = User(name=name, email=email, role=role, **kw)
            u.set_password(PW)
            db.session.add(u)
            return u
        user("Directeur Démo", "direction@prolearning-demo.dz", "admin")
        user("Samira (secrétariat)", "secretariat@prolearning-demo.dz", "secretaire")
        T_weld = user("M. Hichem Boualem — IWE", "soudage@prolearning-demo.dz", "formateur", pay_mode="hour", pay_rate=4000, phone=phone())
        T_lean = user("Mme Samia Lamri — Black Belt", "lean@prolearning-demo.dz", "formateur", pay_mode="seance", pay_rate=15000, phone=phone())
        T_hse = user("M. Rafik Medjdoub — Ingénieur HSE", "hse@prolearning-demo.dz", "formateur", pay_mode="seance", pay_rate=12000, phone=phone())
        T_elec = user("M. Nabil Ziani — Ingénieur électricien", "elec@prolearning-demo.dz", "formateur", pay_mode="hour", pay_rate=3000, phone=phone())
        T_qual = user("Mme Leila Haddad — Auditrice IRCA", "qualite@prolearning-demo.dz", "formateur", pay_mode="seance", pay_rate=12000, phone=phone())
        T_xl = user("M. Karim Ouali", "bureautique@prolearning-demo.dz", "formateur", pay_mode="hour", pay_rate=1500, phone=phone())
        T_math = user("Mme Amina Belhadj", "maths@prolearning-demo.dz", "formateur", pay_mode="hour", pay_rate=1200, phone=phone())
        T_phys = user("M. Sofiane Rahmani", "physique@prolearning-demo.dz", "formateur", pay_mode="seance", pay_rate=2500, phone=phone())
        T_eng = user("Mme Yasmine Kerboua", "anglais@prolearning-demo.dz", "formateur", pay_mode="hour", pay_rate=1500, phone=phone())
        db.session.flush()

        # ── Client companies (fictional) ─────────────────────────────────────────────────────────
        def company(name, kind="company", parent=None, **kw):
            c = Company(name=name, kind=kind, parent_id=parent.id if parent else None, **kw)
            db.session.add(c)
            db.session.flush()
            return c
        atlas = company("Groupe Industriel Atlas Métal", "group", sector="Sidérurgie, tubes et chaudronnerie",
                        contact_name="Mme Lynda Saadi (DRH groupe)", contact_phone=phone(), hr_emails="drh@atlas-metal-demo.dz",
                        report_freq="monthly", address="Zone industrielle, Bordj Bou Arréridj")
        a_tubes = company("Atlas Tubes — Unité BBA", parent=atlas, sector="Tubes soudés", hr_emails="rh.tubes@atlas-metal-demo.dz",
                          report_freq="weekly", contact_name="M. Omar Bellil")
        a_chaud = company("Atlas Chaudronnerie — Sétif", parent=atlas, sector="Chaudronnerie industrielle",
                          hr_emails="rh.chaudronnerie@atlas-metal-demo.dz", report_freq="end")
        a_maint = company("Atlas Maintenance Services", parent=atlas, sector="Maintenance industrielle",
                          hr_emails="rh.maintenance@atlas-metal-demo.dz", report_freq="end")
        sahel = company("Groupe Sahel Électronique", "group", sector="Électronique et électroménager",
                        contact_name="M. Kamel Rezig (DRH)", contact_phone=phone(), hr_emails="drh@sahel-demo.dz", report_freq="monthly")
        s_elec = company("Sahel Électronique — Usine BBA", parent=sahel, sector="Assemblage électronique",
                         hr_emails="formation@sahel-demo.dz", report_freq="weekly")
        s_menager = company("Sahel Électroménager — Mansoura", parent=sahel, sector="Électroménager",
                            hr_emails="rh.mansoura@sahel-demo.dz", report_freq="end")
        plast = company("SARL Bordj Plast Emballages", sector="Plasturgie, emballages", hr_emails="rh@bordjplast-demo.dz",
                        report_freq="monthly", contact_name="Mme Nadia Ferroukhi")
        agro = company("EURL Hodna Agro", sector="Agroalimentaire", hr_emails="grh@hodna-agro-demo.dz", report_freq="end",
                       contact_name="M. Salim Bouaziz")
        for name, email, co in (("Mme Lynda Saadi", "drh@atlas-metal-demo.dz", atlas), ("M. Kamel Rezig", "drh@sahel-demo.dz", sahel),
                                ("Mme Nadia Ferroukhi", "rh@bordjplast-demo.dz", plast)):
            user(name, email, "rh", company_id=co.id)
        db.session.flush()

        domain = {atlas.id: "atlas-metal-demo.dz", a_tubes.id: "atlas-metal-demo.dz", a_chaud.id: "atlas-metal-demo.dz",
                  a_maint.id: "atlas-metal-demo.dz", s_elec.id: "sahel-demo.dz", s_menager.id: "sahel-demo.dz",
                  plast.id: "bordjplast-demo.dz", agro.id: "hodna-agro-demo.dz"}
        prefix = {a_tubes.id: "AT", a_chaud.id: "AC", a_maint.id: "AM", s_elec.id: "SE", s_menager.id: "SM", plast.id: "BP", agro.id: "HA"}
        used = set()

        def person(child=False):
            while True:
                fem = rnd.random() < (.5 if child else .3)
                first, last = rnd.choice(FIRST_F if fem else FIRST_M), rnd.choice(LAST)
                if (first, last) not in used:
                    used.add((first, last))
                    return first, last

        def employee(co, job):
            first, last = person()
            st = Student(first_name=first, last_name=last.upper(), phone=phone(), company_id=co.id, job_title=job,
                         employee_no=f"{prefix[co.id]}-{rnd.randint(1000, 9999)}",
                         email=f"{first.lower().replace(' ', '')}.{last.lower()}@{domain[co.id]}",
                         birth_date=date(T.year - rnd.randint(24, 52), rnd.randint(1, 12), rnd.randint(1, 28)),
                         birth_place=rnd.choice(["Bordj Bou Arréridj", "Sétif", "M'Sila", "Bouira"]),
                         level=rnd.choice(["BTS", "Licence", "Ingénieur", "CAP", "Master", "TS"]))
            db.session.add(st)
            return st

        def individual(child):
            first, last = person(child)
            st = Student(first_name=first, last_name=last.upper(), phone=phone() if not child else None,
                         birth_place="Bordj Bou Arréridj")
            if child:
                st.parent_name, st.parent_phone = f"{rnd.choice(FIRST_M)} {last}", phone()
                st.birth_date = date(T.year - rnd.randint(14, 18), rnd.randint(1, 12), rnd.randint(1, 28))
            else:
                st.email = f"{first.lower().replace(' ', '')}.{last.lower()}{rnd.randint(1, 99)}@gmail.com"
                st.birth_date = date(T.year - rnd.randint(19, 40), rnd.randint(1, 12), rnd.randint(1, 28))
                st.level = rnd.choice(["Licence", "Master", "Terminale", "BTS"])
            db.session.add(st)
            db.session.flush()
            return st

        # ── Formations ───────────────────────────────────────────────────────────────────────────
        def formation(slug, title, trainer, start, end, days, t1, t2, **kw):
            fo = Formation(slug=slug, title=title, trainer=trainer.name, trainer_id=trainer.id, starts_on=start, ends_on=end,
                           start_time=t1, end_time=t2, **kw)
            db.session.add(fo)
            db.session.flush()
            d = start
            while d <= end:
                if d.weekday() in days:
                    db.session.add(Seance(formation_id=fo.id, day=d, time_label=f"{t1} - {t2}"))
                d += timedelta(days=1)
            db.session.flush()
            return fo

        SUN_THU = (6, 0, 1, 2, 3)
        F = {}
        # Entreprises — intra and inter
        F["weld"] = formation("inspecteur-soudage-n2", "Inspecteur de soudage — Niveau 2 (préparation CSWIP 3.1 / IWI)", T_weld,
                              T - timedelta(days=40), T + timedelta(days=30), (6, 1), "08:30", "15:30", status="running",
                              description="Contrôle visuel des soudures, lecture des WPS/PQR, défauts de soudage, normes ISO 5817 et ISO 9606, rédaction des rapports d'inspection.",
                              programme="Procédés de soudage (SMAW, GMAW, GTAW, SAW)\nMétallurgie du soudage\nDéfauts et critères d'acceptation ISO 5817\nWPS, PQR et qualification des soudeurs ISO 9606\nContrôles non destructifs : VT, PT, MT, UT, RT\nRapports d'inspection et cas pratiques en atelier",
                              audience="Contrôleurs qualité, chefs d'équipe soudage, techniciens CND", location="Atelier soudage + salle 4",
                              hours_total=120, seats=12, client_company_id=atlas.id, convention_ref="CONV-ATL-2026-014",
                              min_attendance=85, cert_requires_payment=False, check_mode="inout")
        F["lss_gb"] = formation("lean-six-sigma-green-belt", "Lean Six Sigma — Green Belt", T_lean, T - timedelta(days=28),
                                T + timedelta(days=21), (0, 2), "08:30", "16:00", status="running",
                                description="Conduire un projet d'amélioration DMAIC : réduction de la variabilité, des défauts et des gaspillages.",
                                programme="Définir : charte projet, VOC, SIPOC\nMesurer : cartographie VSM, MSA, capabilité Cp/Cpk\nAnalyser : Ishikawa, Pareto, tests statistiques\nInnover : plans d'expériences, solutions Lean\nContrôler : cartes de contrôle SPC, standardisation\nSoutenance du projet Green Belt",
                                audience="Ingénieurs production, responsables qualité, chefs de ligne", location="Salle 1 + site client",
                                hours_total=80, seats=14, client_company_id=sahel.id, convention_ref="BC-SAHEL-2026-077",
                                min_attendance=80, cert_requires_payment=False, check_mode="inout")
        F["5s"] = formation("methode-5s-management-visuel", "Méthode 5S et management visuel en atelier", T_lean,
                            T - timedelta(days=60), T - timedelta(days=46), (6, 1, 3), "08:30", "12:30", status="done",
                            description="Organiser durablement les postes de travail : trier, ranger, nettoyer, standardiser, maintenir.",
                            programme="Les 5S : Seiri, Seiton, Seiso, Seiketsu, Shitsuke\nDiagnostic 5S d'un atelier\nManagement visuel et standards\nAudit 5S et plan de maintien",
                            audience="Opérateurs, chefs d'équipe, agents de maîtrise", location="Site client — Mansoura", hours_total=24,
                            seats=15, client_company_id=s_menager.id, convention_ref="BC-SAHEL-2026-041", min_attendance=80,
                            cert_requires_payment=False)
        F["cphs"] = formation("cphs-hygiene-securite", "CPHS — Formation des membres du Comité Paritaire d'Hygiène et de Sécurité",
                              T_hse, T - timedelta(days=75), T - timedelta(days=64), (6, 1, 3), "08:30", "15:30", status="done",
                              description="Rôle, missions et fonctionnement du CPHS selon la réglementation algérienne ; analyse des risques et enquête accident.",
                              programme="Cadre réglementaire (loi 88-07, décret 05-09)\nMissions et fonctionnement du CPHS\nÉvaluation des risques professionnels\nEnquête après accident (arbre des causes)\nVisites d'inspection et plan d'action",
                              audience="Membres du CPHS, délégués du personnel, responsables HSE", location="Salle 3", hours_total=30,
                              seats=12, client_company_id=a_tubes.id, convention_ref="CONV-ATL-2026-009", min_attendance=90,
                              cert_requires_payment=False)
        F["iso45001"] = formation("iso-45001-sst", "ISO 45001 — Management de la santé et sécurité au travail (inter-entreprises)",
                                  T_hse, T - timedelta(days=21), T + timedelta(days=14), (5,), "09:00", "16:00", status="running",
                                  description="Comprendre et mettre en œuvre les exigences de l'ISO 45001:2018 dans votre entreprise.",
                                  programme="Structure HLS et exigences ISO 45001\nContexte, leadership et participation des travailleurs\nIdentification des dangers et évaluation des risques\nObjectifs, conformité légale, situations d'urgence\nAudit interne et amélioration",
                                  audience="Responsables HSE, qualité, RH", location="Salle 1", hours_total=35, seats=16,
                                  price_total=45000, installments=1, min_attendance=80, cert_requires_payment=True, for_companies=True)
        F["elec"] = formation("habilitation-electrique-b1v-b2v-br", "Habilitation électrique B1V · B2V · BR (NF C 18-510)", T_elec,
                              T - timedelta(days=9), T + timedelta(days=12), (6, 0, 1), "08:00", "15:00", status="running",
                              description="Travailler en sécurité sur ou au voisinage des installations électriques basse tension.",
                              programme="Dangers de l'électricité et accidents\nZones d'environnement et niveaux d'habilitation\nConsignation et déconsignation\nÉquipements de protection individuelle et collective\nTravaux pratiques sur armoire pédagogique\nÉvaluation théorique et pratique",
                              audience="Électriciens et électromécaniciens de maintenance", location="Atelier électrique", hours_total=42,
                              seats=10, client_company_id=a_maint.id, convention_ref="CONV-ATL-2026-021", min_attendance=100,
                              cert_requires_payment=False, check_mode="inout")
        F["sst"] = formation("sauveteur-secouriste-travail", "Sauveteur Secouriste du Travail (SST)", T_hse, T - timedelta(days=33),
                             T - timedelta(days=31), (0, 1, 2), "08:30", "16:00", status="done",
                             description="Porter secours à une victime d'accident du travail et contribuer à la prévention.",
                             programme="Protéger, examiner, alerter, secourir\nSaignements, étouffement, malaise, brûlures\nRéanimation cardio-pulmonaire et défibrillateur\nPrévention des risques dans l'entreprise",
                             audience="Tout salarié désigné", location="Site client — Hodna Agro", hours_total=14, seats=10,
                             client_company_id=agro.id, convention_ref="BC-HODNA-2026-003", min_attendance=100, cert_requires_payment=False)
        F["lss_yb"] = formation("lean-six-sigma-yellow-belt", "Lean Six Sigma — Yellow Belt", T_lean, T - timedelta(days=14),
                                T + timedelta(days=7), (3,), "08:30", "16:00", status="running",
                                description="Les bases du Lean et de la démarche DMAIC pour participer efficacement aux projets d'amélioration.",
                                programme="Gaspillages (Muda) et flux\nDMAIC en pratique\nOutils de base : Pareto, Ishikawa, 5 pourquoi\nChantier Kaizen",
                                audience="Opérateurs confirmés, techniciens, agents de maîtrise", location="Site client — Bordj Plast",
                                hours_total=28, seats=12, client_company_id=plast.id, convention_ref="BC-BPE-2026-011",
                                min_attendance=80, cert_requires_payment=False)
        F["excel_pro"] = formation("excel-avance-gestionnaires", "Excel avancé pour gestionnaires (Power Query, TCD, tableaux de bord)",
                                   T_xl, T - timedelta(days=18), T + timedelta(days=10), (1, 3), "13:30", "16:30", status="running",
                                   description="Automatiser les reportings : import et nettoyage des données, tableaux croisés, indicateurs.",
                                   programme="Fonctions avancées (RECHERCHEX, SOMME.SI.ENS)\nPower Query : importer et transformer\nTableaux croisés dynamiques\nTableaux de bord et graphiques",
                                   audience="Comptables, contrôleurs de gestion, gestionnaires de stocks", location="Salle informatique",
                                   hours_total=24, seats=12, client_company_id=s_elec.id, convention_ref="BC-SAHEL-2026-090",
                                   min_attendance=80, cert_requires_payment=False)
        F["iso9001"] = formation("auditeur-interne-iso-9001", "Auditeur interne ISO 9001:2015 (inter-entreprises)", T_qual,
                                 T + timedelta(days=16), T + timedelta(days=24), (6, 0, 1), "08:30", "16:00", status="open",
                                 description="Planifier, conduire et rapporter un audit interne qualité selon l'ISO 19011.",
                                 programme="Exigences ISO 9001:2015\nPrincipes d'audit ISO 19011\nPréparer le plan et les listes de contrôle\nConduire l'audit et formuler les écarts\nRapport d'audit et suivi des actions",
                                 audience="Animateurs qualité, futurs auditeurs internes", location="Salle 1", hours_total=21, seats=14,
                                 price_total=38000, installments=1, min_attendance=100, cert_requires_payment=True, auto_confirm=False, for_companies=True)
        # Particuliers
        y0 = T.year if T.month >= 9 else T.year - 1
        sept = date(y0, 9, 14)
        F["maths"] = formation("soutien-maths-4am-bem", "Soutien scolaire — Mathématiques 4AM (BEM)", T_math, sept, date(y0 + 1, 6, 10),
                               (1, 3), "17:00", "18:30", status="running", audience="Élèves de 4e année moyenne",
                               description="Cours de soutien en mathématiques, préparation au BEM.",
                               programme="Calcul littéral\nÉquations et inéquations\nThéorème de Thalès\nStatistiques\nSujets type BEM",
                               location="Salle 2", hours_total=96, seats=20, registration_fee=1000, price_total=27000, installments=9,
                               min_attendance=75)
        F["phys"] = formation("prepa-bac-physique-3as", "Préparation BAC — Physique 3AS", T_phys, sept, date(y0 + 1, 5, 30), (5,),
                              "14:00", "16:00", status="running", audience="Terminales sciences et maths",
                              description="Révisions et sujets types du baccalauréat en physique.", location="Salle 3", hours_total=70,
                              seats=20, registration_fee=1000, price_total=24000, installments=8, min_attendance=70)
        F["eng"] = formation("anglais-b1-adultes", "Anglais — Niveau B1 (adultes)", T_eng, T + timedelta(days=10), T + timedelta(days=95),
                             (0, 2), "18:00", "19:30", status="open", audience="Adultes niveau A2", mode="hybrid",
                             online_url="https://meet.google.com/demo-demo-demo",
                             description="Communication orale et écrite, préparation aux échanges professionnels.",
                             programme="Grammaire B1\nExpression orale\nCompréhension\nRédaction d'e-mails", location="Salle 1",
                             hours_total=36, seats=16, price_total=12000, installments=2, auto_confirm=True)
        F["office"] = formation("bureautique-excel", "Bureautique et Excel (particuliers)", T_xl, T - timedelta(days=35), T + timedelta(days=50),
                                (5,), "09:00", "12:00", status="running", audience="Étudiants, demandeurs d'emploi",
                                description="Word, Excel et PowerPoint. Attestation en fin de formation.",
                                programme="Word : mise en page\nExcel : formules et fonctions\nTableaux croisés dynamiques\nPowerPoint",
                                location="Salle informatique", hours_total=36, seats=14, registration_fee=2000, price_total=18000,
                                installments=3, min_attendance=80, check_mode="inout")

        # ── Participants ─────────────────────────────────────────────────────────────────────────
        def enrol(fo, st, start, pay=True):
            e = Enrollment(formation_id=fo.id, student_id=st.id, status="confirmed", source="entreprise" if st.company_id else
                           rnd.choice(["facebook", "whatsapp", "secretariat", "web"]),
                           created_at=datetime.combine(start - timedelta(days=rnd.randint(3, 20)), datetime.min.time()))
            db.session.add(e)
            db.session.flush()
            if pay and (fo.price_total or fo.registration_fee):
                items = []
                if fo.registration_fee:
                    items.append((0, "Frais d'inscription", start, fo.registration_fee))
                k = fo.installments or 1
                base, rest = divmod(fo.price_total, k)
                for i in range(k):
                    items.append((i + 1, "Paiement unique" if k == 1 else f"Mensualité {i + 1}/{k}", add_months(start, i),
                                  base + (rest if i == 0 else 0)))
                late = rnd.random() < .15
                for num, label, due, amount in items:
                    p = Payment(enrollment_id=e.id, number=num, label=label, due_on=due, amount=amount)
                    if due <= T and (not late or due < T - timedelta(days=40)):
                        p.status, p.paid_on = "paid", min(T, due + timedelta(days=rnd.randint(0, 5)))
                        p.method = "virement" if st.company_id else rnd.choice(["cash", "cash", "ccp"])
                        p.collected_by = "Secrétariat"
                    db.session.add(p)
                db.session.flush()
                for p in Payment.query.filter_by(enrollment_id=e.id, status="paid"):
                    p.receipt_no = f"R{p.paid_on.year}-{p.id:05d}"
            return e

        groups = {
            "weld": [(a_tubes, ["Contrôleur qualité soudure", "Chef d'équipe soudage", "Technicien CND", "Soudeur qualifié"], 6),
                     (a_chaud, ["Chef d'atelier chaudronnerie", "Contrôleur qualité soudure", "Soudeur TIG"], 4)],
            "lss_gb": [(s_elec, ["Ingénieur production", "Responsable qualité", "Chef de ligne SMT", "Ingénieur méthodes"], 7),
                       (s_menager, ["Responsable production", "Ingénieur qualité", "Chef de ligne"], 4)],
            "5s": [(s_menager, ["Opérateur", "Chef d'équipe", "Agent de maîtrise", "Magasinier"], 12)],
            "cphs": [(a_tubes, ["Membre CPHS — représentant du personnel", "Membre CPHS — représentant employeur", "Responsable HSE", "Chef d'atelier"], 8)],
            "elec": [(a_maint, ["Électricien de maintenance", "Électromécanicien", "Technicien automatisme"], 8)],
            "sst": [(agro, ["Opérateur de production", "Chef d'équipe", "Agent de conditionnement", "Cariste"], 10)],
            "lss_yb": [(plast, ["Opérateur extrusion", "Technicien qualité", "Chef d'équipe", "Régleur"], 9)],
            "excel_pro": [(s_elec, ["Comptable", "Contrôleur de gestion", "Gestionnaire de stocks", "Acheteur"], 8)],
            "iso45001": [(plast, ["Responsable HSE", "Animateur qualité"], 2), (agro, ["Responsable HSE", "Responsable qualité"], 3)],
        }
        E = {}
        staff = {}
        for key, parts in groups.items():
            fo = F[key]
            E[key] = []
            for co, jobs, n in parts:
                pool = staff.setdefault(co.id, [])
                for i in range(n):
                    job = jobs[i % len(jobs)]
                    reuse = [s for s in pool if s.job_title == job and all(e.student_id != s.id for e in E[key])]
                    st = reuse[0] if reuse and rnd.random() < .35 else employee(co, job)
                    if st not in pool:
                        pool.append(st)
                    db.session.flush()
                    E[key].append(enrol(fo, st, fo.starts_on))
        # a few employees not yet trained (shows "salariés suivis x / y")
        for co, job in ((a_tubes, "Soudeur qualifié"), (s_elec, "Technicien de test"), (plast, "Opérateur extrusion")):
            for _ in range(2):
                employee(co, job)
        for key, n, child in (("maths", 16, True), ("phys", 14, True), ("office", 11, False), ("eng", 6, False)):
            fo = F[key]
            E[key] = [enrol(fo, individual(child), fo.starts_on) for _ in range(n)]
        db.session.flush()

        # ── Attendance (realistic, a few weak attendees) ─────────────────────────────────────────
        def attendance(fo, enrs, weak=()):
            for s in fo.seances.filter(Seance.day <= T).all():
                a, _b = s.bounds()
                for e in enrs:
                    r = rnd.random()
                    if e.id in weak:
                        st = "absent" if r < .35 else "late" if r < .5 else "present"
                    else:
                        st = "present" if r < .86 else "late" if r < .93 else "excused" if r < .96 else "absent"
                    att = Attendance(seance_id=s.id, enrollment_id=e.id, status=st)
                    if st in ("present", "late"):
                        mins = a - rnd.randint(2, 12) if st == "present" else a + rnd.randint(17, 40)
                        att.checkin_at = datetime(s.day.year, s.day.month, s.day.day) + timedelta(minutes=mins - 60)
                        att.method = rnd.choice(["ticket", "ticket", "room", "manual"])
                    db.session.add(att)
        for key, enrs in E.items():
            if key == "eng":
                continue
            weak = {enrs[2].id} if len(enrs) > 5 and key not in ("sst", "cphs") else set()
            attendance(F[key], enrs, weak)
        # certificates already issued for finished company sessions
        for key in ("5s", "cphs", "sst"):
            fo = F[key]
            for e in E[key]:
                e.cert_issued_on = fo.ends_on + timedelta(days=3)
        db.session.flush()
        for key in ("5s", "cphs", "sst"):
            for e in E[key]:
                e.cert_no = f"ATT-{e.cert_issued_on.year}-{e.id:05d}"

        # ── Money: conventions (companies pay per session), expenses, trainer pay ────────────────
        for key, amount, label in (("cphs", 420000, "Convention CPHS — Atlas Tubes"), ("5s", 360000, "5S — Sahel Électroménager"),
                                   ("sst", 150000, "SST — Hodna Agro"), ("weld", 1250000, "Acompte 50 % inspecteur soudage — Atlas Métal"),
                                   ("lss_gb", 980000, "Acompte Lean Six Sigma Green Belt — Sahel"),
                                   ("excel_pro", 240000, "Excel avancé — Sahel Électronique")):
            fo = F[key]
            db.session.add(CashEntry(kind="in", category="convention", amount=amount, on=min(T, fo.starts_on + timedelta(days=10)),
                                     method="virement", label=label, ref=fo.convention_ref, created_by="Directeur Démo"))
        m0 = T.replace(day=1)
        for k in range(5, -1, -1):
            a = add_months(m0, -k)

            def out(cat, amount, day, label, method="cash"):
                d = a.replace(day=min(day, 28))
                if d <= T:
                    db.session.add(CashEntry(kind="out", category=cat, amount=amount, on=d, method=method, label=label,
                                             created_by="Directeur Démo"))
            out("loyer", 90000, 5, "Loyer des locaux", "virement")
            out("energie", rnd.randint(12, 20) * 1000, 18, "Sonelgaz / ADE", "ccp")
            out("telecom", 6500, 10, "Fibre + téléphones", "ccp")
            out("fournitures", rnd.randint(8, 25) * 1000, 12, "Consommables atelier soudage et salles")
            out("transport", rnd.randint(15, 30) * 1000, 22, "Déplacements formateurs sur sites clients")
            if a < m0:
                for u, amount in ((T_weld, 96000), (T_lean, 120000), (T_hse, 72000), (T_xl, 30000), (T_math, 18000), (T_phys, 12500)):
                    db.session.add(CashEntry(kind="out", category="salaire", amount=amount + rnd.randint(-4, 4) * 1000,
                                             teacher_id=u.id, period=f"{a:%Y-%m}", on=min(T, add_months(a, 1) - timedelta(days=1)),
                                             method="virement", created_by="Directeur Démo"))
        db.session.add(AuditLog(user="system", action="demo_seed", details="École de démonstration AcadémieProLearning"))
        db.session.commit()
        return sc
    finally:
        g.school_id, g.unscoped = prev


if __name__ == "__main__":
    import os
    os.environ.setdefault("DISABLE_SCHEDULER", "1")
    from app import app, today
    with app.app_context():
        sc = run(today())
        print("✅ École de démonstration créée." if sc else "ℹ️  L'école de démonstration existe déjà.")
        print("   direction@prolearning-demo.dz / demo1234 · RH : drh@atlas-metal-demo.dz / demo1234")
