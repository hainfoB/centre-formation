"""Cours en ligne de démonstration (idempotent) pour l'école « institut-prolearning-demo »."""
import io
import random
from datetime import datetime, timedelta

from models import Activity, ActivityCompletion, Course, CourseSection, Formation, School, StoredFile, db


def _pdf(title, subtitle, blocks):
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    c.setFillColor(HexColor("#0b2545")); c.rect(0, H - 90, W, 90, fill=1, stroke=0)
    c.setFillColor(HexColor("#f97316")); c.rect(0, H - 94, W, 4, fill=1, stroke=0)
    c.setFillColor(white); c.setFont("Helvetica-Bold", 18); c.drawString(40, H - 50, title)
    c.setFont("Helvetica", 10); c.drawString(40, H - 72, subtitle)
    y = H - 130
    for head, lines in blocks:
        if y < 120:
            c.showPage(); y = H - 60
        c.setFillColor(HexColor("#f97316")); c.setFont("Helvetica-Bold", 12.5); c.drawString(40, y, head); y -= 20
        c.setFillColor(HexColor("#1d2b45")); c.setFont("Helvetica", 10.5)
        for ln in lines:
            c.drawString(52, y, "• " + ln); y -= 16
        y -= 10
    c.setFillColor(HexColor("#7a869a")); c.setFont("Helvetica", 8)
    c.drawString(40, 30, "Institut ProLearning — document pédagogique de démonstration")
    c.save()
    return buf.getvalue()


def add(slug="institut-prolearning-demo"):
    from flask import g
    from tenancy import unscoped
    with unscoped():
        sc = School.query.filter_by(slug=slug).first()
        if not sc or Course.query.filter_by(school_id=sc.id).first():
            return 0
    prev = g.get("school_id"), g.get("unscoped", False)
    g.school_id, g.unscoped = sc.id, False
    rnd = random.Random(7)
    try:
        def fo(s):
            return Formation.query.filter_by(slug=s).first()

        def course(f, title, summary, sections, sequential=False):
            if not f:
                return None
            c = Course(formation_id=f.id, title=title, summary=summary, status="published", sequential=sequential)
            db.session.add(c); db.session.flush()
            for i, (stitle, ssum, acts) in enumerate(sections):
                s = CourseSection(course_id=c.id, position=i, title=stitle, summary=ssum)
                db.session.add(s); db.session.flush()
                for j, a in enumerate(acts):
                    kind, title_, body = a[0], a[1], a[2]
                    act = Activity(course_id=c.id, section_id=s.id, position=j, kind=kind, title=title_, body=body,
                                   duration_min=a[3] if len(a) > 3 else None, completion="none" if kind == "label" else
                                   (a[4] if len(a) > 4 else "view"))
                    if kind == "file":
                        data = _pdf(title_, f.title, a[5])
                        sf = StoredFile(name=title_.replace(" ", "_")[:60] + ".pdf", mime="application/pdf", size=len(data), data=data)
                        db.session.add(sf); db.session.flush()
                        act.file_id = sf.id
                    if kind == "url":
                        act.url = a[5]
                    db.session.add(act)
            db.session.flush()
            return c

        W = course(fo("inspecteur-soudage-n2"), "Inspecteur de soudage — parcours en ligne",
                   "Révisez entre les journées en atelier : procédés, défauts, normes et documents de soudage. Chaque module s'ouvre quand le précédent est terminé.",
                   [("Bienvenue", "Comment suivre ce parcours.", [
                       ("label", "Bienvenue", "**Bienvenue dans le parcours en ligne.** Comptez environ 30 minutes par module. Votre progression est visible par votre formateur et par le service RH de votre entreprise."),
                       ("page", "Objectifs et organisation", "# Ce que vous saurez faire\n- Identifier les principaux **procédés de soudage** et leurs paramètres\n- Reconnaître les **défauts** et appliquer les critères de l'**ISO 5817**\n- Lire une **WPS** et vérifier la qualification d'un soudeur (**ISO 9606**)\n- Rédiger un **rapport d'inspection** clair\n\n# Organisation\n1. Lisez chaque page et téléchargez les fiches.\n2. Cochez « J'ai terminé » quand c'est demandé.\n3. Les modules s'ouvrent l'un après l'autre.\n\n> Astuce : gardez les fiches PDF sur votre téléphone pour les consulter en atelier.", 8, "manual")]),
                    ("Module 1 — Les procédés de soudage", "SMAW, GMAW, GTAW, SAW : principes et paramètres.", [
                       ("page", "Les quatre procédés à connaître", "# Soudage à l'électrode enrobée (SMAW / 111)\nProcédé polyvalent, utilisé sur chantier. L'enrobage protège le bain et forme le laitier.\n\n# MIG/MAG (GMAW / 131-135)\nFil continu et gaz de protection : productif, adapté à l'atelier.\n\n# TIG (GTAW / 141)\nÉlectrode de tungstène non fusible : soudures propres, passes de racine, inox et aluminium.\n\n# Arc submergé (SAW / 121)\nForte productivité sur fortes épaisseurs (tubes, réservoirs).\n\n# Paramètres à contrôler\n- Intensité et tension\n- Vitesse de soudage et **énergie de soudage** (kJ/mm)\n- Préchauffage et température entre passes\n- Nature et débit du gaz", 20),
                       ("file", "Fiche — Procédés et paramètres", "Fiche récapitulative à télécharger.", 5, "view",
                        [("Procédés", ["111 : électrode enrobée", "131/135 : MIG / MAG", "141 : TIG", "121 : arc submergé"]),
                         ("Énergie de soudage", ["E = k × U × I × 60 / (1000 × v)", "k = rendement thermique (0,6 à 1)", "U en V, I en A, v en mm/min"]),
                         ("Points de contrôle", ["Préchauffage", "Température entre passes", "Gaz : nature et débit", "Propreté des chanfreins"])]),
                       ("page", "Auto-évaluation du module 1", "# Vérifiez vos acquis\n1. Quel procédé utilise une électrode non fusible ?\n2. Pourquoi contrôle-t-on la température entre passes ?\n3. Citez deux paramètres qui influencent l'énergie de soudage.\n\n*Les réponses sont discutées lors de la prochaine journée en atelier.*", 10, "manual")]),
                    ("Module 2 — Défauts et critères ISO 5817", "Reconnaître, nommer et juger un défaut.", [
                       ("page", "Les familles de défauts", "# Les six familles (ISO 6520-1)\n1. **Fissures** — toujours rédhibitoires\n2. **Cavités** : soufflures, piqûres\n3. **Inclusions solides** : laitier, tungstène\n4. **Manque de fusion et de pénétration**\n5. **Défauts de forme** : caniveau, surépaisseur, effondrement\n6. **Défauts divers** : projections, coups d'arc\n\n# Niveaux de qualité ISO 5817\n- **B** : exigence élevée\n- **C** : exigence moyenne\n- **D** : exigence modérée", 25),
                       ("file", "Catalogue illustré des défauts", "Catalogue à garder en atelier.", 10, "view",
                        [("Défauts plans", ["Fissure longitudinale / transversale", "Manque de fusion latérale", "Manque de pénétration"]),
                         ("Défauts volumiques", ["Soufflure sphéroïdale", "Inclusion de laitier", "Nid de soufflures"]),
                         ("Défauts de forme", ["Caniveau", "Surépaisseur excessive", "Désalignement"])])]),
                    ("Module 3 — Documents de soudage", "WPS, PQR et qualification des soudeurs.", [
                       ("page", "Lire une WPS", "# Le descriptif de mode opératoire (WPS)\nLa WPS fixe les conditions de soudage : procédé, matériaux, épaisseurs, position, métal d'apport, paramètres électriques, préchauffage.\n\n# Le PQR\nLe procès-verbal de qualification prouve, par des essais, que la WPS donne une soudure conforme.\n\n# La qualification du soudeur (ISO 9606-1)\nVérifiez sur le certificat : procédé, type de produit, position, épaisseur, et **la date de validité**.", 20),
                       ("url", "Référence : le site de l'ISO", "Retrouvez la liste des normes citées (ISO 5817, ISO 9606-1, ISO 15614-1) sur le site officiel.", 5, "view", "https://www.iso.org")]),
                    ("Évaluation finale", "Le quiz final sera disponible avec le module évaluation.", [
                       ("label", "À venir", "Le **quiz final** et le **rapport d'inspection à rendre** seront ajoutés avec le module d'évaluation.")])],
                   sequential=True)

        L = course(fo("lean-six-sigma-green-belt"), "Lean Six Sigma Green Belt — parcours DMAIC",
                   "Les cinq phases DMAIC avec fiches outils et modèle de charte projet.",
                   [("Démarrer", None, [("page", "La démarche DMAIC en une page", "# Définir, Mesurer, Analyser, Innover, Contrôler\nChaque phase se termine par une **revue de passage** avec votre sponsor.\n\n- **D** : charte projet, voix du client, SIPOC\n- **M** : cartographie, collecte, capabilité\n- **A** : causes racines, tests\n- **I** : solutions, pilote\n- **C** : standards, cartes de contrôle", 12)]),
                    ("Définir", None, [("file", "Modèle de charte projet", "À compléter pour votre projet.", 15, "manual",
                                       [("Charte projet", ["Problème et périmètre", "Objectif chiffré (SMART)", "Gains attendus", "Équipe et sponsor", "Jalons DMAIC"])]),
                                      ("page", "SIPOC et voix du client", "# SIPOC\nFournisseurs, Entrées, Processus, Sorties, Clients : une vue d'ensemble avant de mesurer.\n\n# Voix du client\nTransformez chaque attente en **caractéristique mesurable (CTQ)**.", 15)]),
                    ("Mesurer", None, [("page", "Capabilité Cp et Cpk", "# Indicateurs\n- **Cp** = (LSS − LSI) / 6σ\n- **Cpk** = min(LSS − μ ; μ − LSI) / 3σ\n\nUn procédé est jugé capable quand **Cpk ≥ 1,33**.", 20)]),
                    ("Analyser et innover", None, [("page", "Ishikawa et 5 pourquoi", "# Diagramme d'Ishikawa (5M)\nMain-d'œuvre, Méthodes, Matières, Machines, Milieu.\n\n# 5 pourquoi\nPosez « pourquoi ? » jusqu'à la cause racine actionnable.", 15)])])

        M = course(fo("soutien-maths-4am-bem"), "Maths 4AM — révisions BEM",
                   "Fiches de cours et séries d'exercices pour réviser à la maison.",
                   [("Calcul littéral", None, [("page", "Développer et factoriser", "# Identités remarquables\n- (a + b)² = a² + 2ab + b²\n- (a − b)² = a² − 2ab + b²\n- (a + b)(a − b) = a² − b²\n\n# À toi\n1. Développe (2x + 3)²\n2. Factorise 9x² − 16", 20, "manual")]),
                    ("Théorème de Thalès", None, [("file", "Série d'exercices — Thalès", "Série à faire avant la séance de jeudi.", 30, "manual",
                                                  [("Exercice 1", ["Dans le triangle ABC, (MN) // (BC).", "AM = 3 cm, AB = 7,5 cm, BC = 10 cm.", "Calcule MN."]),
                                                   ("Exercice 2", ["Démontre que deux droites sont parallèles", "en utilisant la réciproque du théorème."])])]),
                    ("Statistiques", None, [("page", "Moyenne, médiane, étendue", "# À retenir\n- **Moyenne** : somme des valeurs ÷ effectif\n- **Médiane** : valeur du milieu de la série rangée\n- **Étendue** : plus grande valeur − plus petite", 15)])])

        C = course(fo("cphs-hygiene-securite"), "CPHS — ressources du comité",
                   "Documents de référence pour les membres du comité après la formation.",
                   [("Ressources", None, [("page", "Missions du CPHS", "# Les missions\n- Veiller à l'application des règles d'hygiène et de sécurité\n- Analyser les risques et proposer des mesures\n- Enquêter après chaque accident du travail\n- Réaliser des visites d'inspection régulières", 10),
                                          ("file", "Modèle — compte rendu d'enquête accident", "Modèle à utiliser en réunion.", 10, "view",
                                           [("Enquête accident", ["Faits : date, lieu, victime", "Arbre des causes", "Mesures immédiates", "Plan d'action : responsable et délai"])])])])

        db.session.flush()
        # learners' progress
        for c in (x for x in (W, L, M, C) if x):
            acts = [a for a in c.activities(True) if a.kind != "label"]
            for e in c.formation.enrollments.filter_by(status="confirmed"):
                k = int(len(acts) * rnd.choice([0, .2, .4, .6, .8, 1, 1, .6]))
                for a in acts[:k]:
                    t = datetime.utcnow() - timedelta(days=rnd.randint(0, 20), hours=rnd.randint(0, 10))
                    db.session.add(ActivityCompletion(activity_id=a.id, enrollment_id=e.id, viewed_at=t, completed_at=t))
        db.session.commit()
        return 4
    finally:
        g.school_id, g.unscoped = prev


if __name__ == "__main__":
    import os
    os.environ.setdefault("DISABLE_SCHEDULER", "1")
    from app import app
    with app.app_context():
        print("cours créés :", add())
