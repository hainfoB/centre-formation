"""Quiz, devoirs et tentatives de démonstration (idempotent)."""
import json
import random
from datetime import datetime, timedelta

from models import Activity, ActivityCompletion, Course, CourseSection, Question, QuizAttempt, School, Submission, db

WELD = """$CATEGORY: Procédés
Quel procédé utilise une électrode de tungstène non fusible ? {=TIG (141) ~MAG (135) ~Électrode enrobée (111) ~Arc submergé (121)}

Le procédé 121 (arc submergé) est surtout utilisé pour : {=Les fortes épaisseurs en atelier ~Les tôles très fines ~Le soudage en position au plafond}

La température entre passes doit être contrôlée. {T}

Associez chaque numéro ISO 4063 à son procédé : {=111 -> Électrode enrobée =131 -> MIG =141 -> TIG =121 -> Arc submergé}

$CATEGORY: Défauts
Une fissure est-elle acceptable au niveau de qualité D de l'ISO 5817 ? {F}

Quels défauts sont des défauts volumiques ? {~%50%Soufflure ~%50%Inclusion de laitier ~%-100%Fissure ~%-100%Caniveau}

Dans l'ISO 5817, quel niveau de qualité est le plus exigeant ? {=B ~C ~D}

Comment appelle-t-on le creux le long du pied du cordon, dans le métal de base ? {=caniveau =caniveaux}

$CATEGORY: Documents
Le document qui fixe les paramètres de soudage à appliquer s'appelle : {=La WPS (descriptif de mode opératoire) ~Le PQR ~Le certificat matière}

Un soudeur qualifié ISO 9606-1 en 2024, avec prolongation tous les 6 mois, doit faire revalider sa qualification au plus tard après combien d'années ? {#3:0}
"""
LEAN = """$CATEGORY: DMAIC
Dans quelle phase établit-on la charte projet ? {=Définir ~Mesurer ~Analyser ~Contrôler}

Un procédé est généralement jugé capable lorsque Cpk est au moins égal à : {#1.33:0.01}

Le diagramme d'Ishikawa sert à : {=Rechercher les causes d'un problème ~Planifier un projet ~Mesurer la capabilité}

Les cartes de contrôle SPC sont utilisées principalement en phase Contrôler. {T}

Associez l'outil à sa phase : {=SIPOC -> Définir =MSA -> Mesurer =5 pourquoi -> Analyser =Standard de travail -> Contrôler}

Citez le M des 5M qui désigne l'environnement de travail. {=Milieu}
"""
MATHS = """$CATEGORY: Calcul
Développe (x + 3)² : {=x² + 6x + 9 ~x² + 9 ~x² + 3x + 9}

Combien vaut 2³ × 5 ? {#40:0}

Factorise 9x² − 16 : {=(3x − 4)(3x + 4) ~(9x − 4)(x + 4) ~(3x − 16)(3x + 1)}

$CATEGORY: Géométrie
Si (MN) // (BC), AM = 3, AB = 7,5 et BC = 10, combien vaut MN ? {#4:0.01}

Dans un triangle rectangle, le côté opposé à l'angle droit s'appelle : {=l'hypoténuse =hypoténuse =hypotenuse}

La réciproque du théorème de Thalès permet de démontrer que deux droites sont parallèles. {T}
"""


def add(slug="institut-prolearning-demo"):
    from flask import g
    import assess
    from tenancy import unscoped
    with unscoped():
        sc = School.query.filter_by(slug=slug).first()
        if not sc or Question.query.filter_by(school_id=sc.id).first():
            return 0
    prev = g.get("school_id"), g.get("unscoped", False)
    g.school_id, g.unscoped = sc.id, False
    rnd = random.Random(11)
    try:
        def course(prefix):
            return Course.query.filter(Course.title.like(prefix + "%")).first()

        def bank(c, gift):
            qs = []
            for cat, qt, text, data, fb in assess.parse_gift(gift):
                q = Question(course_id=c.id, category=cat, qtype=qt, text=text, data=json.dumps(data, ensure_ascii=False), feedback=fb)
                db.session.add(q)
                qs.append(q)
            db.session.flush()
            return qs

        def last_section(c, title=None):
            secs = c.sections.all()
            if title:
                s = CourseSection(course_id=c.id, position=len(secs), title=title)
                db.session.add(s)
                db.session.flush()
                return s
            return secs[-1]

        def activity(c, s, kind, title, body, opts, minutes):
            a = Activity(course_id=c.id, section_id=s.id, position=s.activities.count(), kind=kind, title=title, body=body,
                         completion="auto", duration_min=minutes)
            a.set_opts(opts)
            db.session.add(a)
            db.session.flush()
            return a

        made = []
        W = course("Inspecteur de soudage")
        if W:
            qs = bank(W, WELD)
            s = last_section(W)
            for a in s.activities.filter_by(kind="label").all():
                db.session.delete(a)
            s.summary = "Quiz final et rapport d'inspection à rendre."
            q1 = activity(W, s, "quiz", "Quiz final — inspecteur de soudage",
                          "10 questions tirées au hasard dans la banque. **20 minutes**, 2 tentatives, 70 % pour réussir.",
                          {"mode": "random", "random_n": 10, "category": "", "time_limit": 20, "attempts": 2, "pass_pct": 70,
                           "shuffle": True, "review": "after", "grade_method": "best", "ids": []}, 20)
            due = (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%dT18:00")
            a1 = activity(W, s, "assign", "Rapport d'inspection visuelle d'un assemblage",
                          "Examinez l'éprouvette remise en atelier et rédigez un **rapport d'inspection** :\n1. Identification de l'assemblage\n2. Défauts relevés (ISO 6520-1)\n3. Décision selon l'ISO 5817, niveau C\n4. Recommandations\n\nRemettez un fichier PDF ou Word.",
                          {"due": due, "max_points": 20, "allow_late": True, "mode": "both"}, 60)
            made.append((W, q1, a1))
        L = course("Lean Six Sigma Green Belt")
        if L:
            qs = bank(L, LEAN)
            s = last_section(L, "Évaluation")
            q2 = activity(L, s, "quiz", "Quiz — les outils DMAIC", "6 questions, sans limite de temps, 3 tentatives.",
                          {"mode": "pick", "ids": [q.id for q in qs], "time_limit": 0, "attempts": 3, "pass_pct": 60,
                           "shuffle": True, "review": "after", "grade_method": "best"}, 10)
            a2 = activity(L, s, "assign", "Charte de votre projet Green Belt",
                          "Complétez le modèle de charte projet pour votre propre chantier et déposez-le ici.",
                          {"due": (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT23:59"), "max_points": 20,
                           "allow_late": False, "mode": "file"}, 45)
            made.append((L, q2, a2))
        M = course("Maths 4AM")
        if M:
            qs = bank(M, MATHS)
            s = last_section(M, "Je m'entraîne")
            q3 = activity(M, s, "quiz", "Quiz d'entraînement BEM", "6 questions, 15 minutes. Tentatives illimitées : entraîne-toi !",
                          {"mode": "pick", "ids": [q.id for q in qs], "time_limit": 15, "attempts": 0, "pass_pct": 50,
                           "shuffle": True, "review": "after", "grade_method": "best"}, 15)
            made.append((M, q3, None))
        db.session.flush()

        # attempts and submissions by the learners who are far enough in the course
        for c, quiz, assign in made:
            bankq = Question.query.filter_by(course_id=c.id).all()
            for e in c.formation.enrollments.filter_by(status="confirmed"):
                n = ActivityCompletion.query.filter_by(enrollment_id=e.id).count()
                if n < 3 or rnd.random() < .25:
                    continue
                level = rnd.choice([.45, .6, .75, .85, .95])
                ids = [q.id for q in bankq][: quiz.opts().get("random_n") or len(bankq)]
                ans, score, mx = {}, 0.0, 0.0
                for q in bankq:
                    if q.id not in ids:
                        continue
                    d = q.d()
                    good = rnd.random() < level
                    if q.qtype == "mcq":
                        a = next((i for i, o in enumerate(d["options"]) if o["ok"] == good), 0)
                    elif q.qtype == "multi":
                        a = [str(i) for i, o in enumerate(d["options"]) if o["ok"]] if good else ["0"]
                    elif q.qtype == "tf":
                        a = ("1" if d["answer"] else "0") if good else ("0" if d["answer"] else "1")
                    elif q.qtype == "short":
                        a = d["answers"][0] if good else "?"
                    elif q.qtype == "numeric":
                        a = str(d["answer"] if good else d["answer"] + 1)
                    else:
                        a = {str(i): (p[1] if good else "") for i, p in enumerate(d["pairs"])}
                    ans[str(q.id)] = a
                    e_, _f = assess.grade_question(q, a)
                    score += e_
                    mx += q.points or 1
                t = datetime.utcnow() - timedelta(days=rnd.randint(0, 6))
                pct = round(score * 100 / mx, 1) if mx else 0
                db.session.add(QuizAttempt(activity_id=quiz.id, enrollment_id=e.id, number=1, started_at=t - timedelta(minutes=12),
                                           finished_at=t, layout=json.dumps({"q": ids, "o": {}}), answers=json.dumps(ans, ensure_ascii=False),
                                           score=round(score, 2), max_score=mx, pct=pct))
                if pct >= quiz.opts().get("pass_pct", 0):
                    db.session.add(ActivityCompletion(activity_id=quiz.id, enrollment_id=e.id, viewed_at=t, completed_at=t))
                if assign and rnd.random() < .6:
                    graded = rnd.random() < .5
                    db.session.add(Submission(activity_id=assign.id, enrollment_id=e.id, submitted_at=t,
                                              text="Rapport joint : défauts relevés, décision et recommandations détaillées.",
                                              grade=rnd.choice([11, 12.5, 14, 15.5, 17]) if graded else None,
                                              feedback="Bon travail, précisez la référence des critères." if graded else None,
                                              graded_at=t if graded else None, graded_by="Formateur" if graded else None))
                    db.session.add(ActivityCompletion(activity_id=assign.id, enrollment_id=e.id, viewed_at=t, completed_at=t))
        db.session.commit()
        return len(made)
    finally:
        g.school_id, g.unscoped = prev


if __name__ == "__main__":
    import os
    os.environ.setdefault("DISABLE_SCHEDULER", "1")
    from app import app
    with app.app_context():
        print("évaluations :", add())
