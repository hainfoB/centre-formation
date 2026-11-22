"""Annonces, forums et sondages de démonstration (idempotent)."""
import json
import random
from datetime import datetime, timedelta

from models import Activity, ActivityCompletion, Announcement, Course, CourseSection, ForumPost, PollResponse, School, db

HOT = [{"text": "Globalement, êtes-vous satisfait(e) de cette formation ?", "type": "scale", "options": []},
       {"text": "Les contenus correspondent-ils à votre poste de travail ?", "type": "scale", "options": []},
       {"text": "Le formateur maîtrise-t-il son sujet et l'explique-t-il clairement ?", "type": "scale", "options": []},
       {"text": "Le rythme de la formation était :", "type": "choice", "options": ["Trop lent", "Adapté", "Trop rapide"]},
       {"text": "Qu'allez-vous appliquer dès votre retour au poste ?", "type": "text", "options": []},
       {"text": "Recommanderiez-vous cette formation à un collègue ?", "type": "choice", "options": ["Oui", "Non", "Je ne sais pas"]}]
TEXTS = ["Le contrôle visuel systématique avant la radiographie.", "Utiliser le catalogue des défauts en atelier.",
         "Mieux lire les WPS avant de lancer une série.", "Vérifier les qualifications des soudeurs sous-traitants.",
         "Mettre en place une check-list d'inspection.", "Plus de cas pratiques sur éprouvettes, svp."]


def add(slug="institut-prolearning-demo"):
    from flask import g
    from tenancy import unscoped
    with unscoped():
        sc = School.query.filter_by(slug=slug).first()
        if not sc or Announcement.query.filter_by(school_id=sc.id).first():
            return 0
    prev = g.get("school_id"), g.get("unscoped", False)
    g.school_id, g.unscoped = sc.id, False
    rnd = random.Random(5)
    try:
        now = datetime.utcnow()
        for c in Course.query.all():
            enrs = c.formation.enrollments.filter_by(status="confirmed").all()
            trainer = c.formation.trainer or "Formateur"
            if c.title.startswith("Inspecteur"):
                db.session.add(Announcement(course_id=c.id, title="Journée atelier : apportez vos EPI", pinned=True, author=trainer,
                                            body="La séance de **dimanche** se déroule à l'atelier soudage. Casque, gants et lunettes obligatoires.",
                                            created_at=now - timedelta(days=2)))
                db.session.add(Announcement(course_id=c.id, title="Le quiz final est ouvert", author=trainer,
                                            body="Vous avez **2 tentatives** et 20 minutes. Bonne chance !", created_at=now - timedelta(days=1)))
            elif enrs:
                db.session.add(Announcement(course_id=c.id, title="Bienvenue dans votre cours en ligne", author=trainer,
                                            body="Commencez par la première section. Posez vos questions dans le forum.", created_at=now - timedelta(days=6)))
            sec = c.sections.first()
            if not sec or not enrs:
                continue
            fa = Activity(course_id=c.id, section_id=sec.id, position=sec.activities.count(), kind="forum", title="Forum des questions",
                          body="Posez ici vos questions sur le cours. Le formateur répond sous 48 h.", completion="none")
            fa.set_opts({"type": "general"})
            db.session.add(fa)
            db.session.flush()
            if c.title.startswith("Inspecteur"):
                qs = [("Différence entre niveau B et C ?", "Pour un réservoir sous pression, on applique quel niveau de l'ISO 5817 ?",
                       "Le niveau est fixé par le code de construction ou le client. Pour les appareils à pression, c'est souvent B ou C : vérifiez le cahier des charges."),
                      ("Validité de la qualification soudeur", "Un soudeur qui n'a pas soudé pendant 7 mois garde-t-il sa qualification ?",
                       "Non : au-delà de 6 mois sans soudage dans le procédé, la qualification doit être renouvelée (ISO 9606-1, §9.2)."),
                      ("Fiche défauts en arabe ?", "Est-ce possible d'avoir le catalogue des défauts en arabe pour les soudeurs ?", None)]
            else:
                qs = [("Question sur le premier module", "Pouvez-vous redonner un exemple vu en séance ?", "Oui, je l'ajoute dans la page du module.")]
            for i, (subj, body, answer) in enumerate(qs):
                e = enrs[i % len(enrs)]
                t = ForumPost(activity_id=fa.id, enrollment_id=e.id, author=e.student.full_name, subject=subj, body=body,
                              created_at=now - timedelta(days=5 - i, hours=3))
                db.session.add(t)
                db.session.flush()
                if answer:
                    db.session.add(ForumPost(activity_id=fa.id, parent_id=t.id, user_id=c.formation.trainer_id, author=f"{trainer} (formateur)",
                                             body=answer, created_at=t.created_at + timedelta(hours=5)))
                    if len(enrs) > 2:
                        e2 = enrs[(i + 2) % len(enrs)]
                        db.session.add(ForumPost(activity_id=fa.id, parent_id=t.id, enrollment_id=e2.id, author=e2.student.full_name,
                                                 body="Merci, c'est plus clair.", created_at=t.created_at + timedelta(hours=8)))
            if c.formation.is_pro:
                last = c.sections.order_by(CourseSection.position.desc()).first()
                pa = Activity(course_id=c.id, section_id=last.id, position=last.activities.count() + 5, kind="poll",
                              title="Évaluation à chaud de la formation", completion="auto",
                              body="Deux minutes pour nous aider à améliorer la formation. Vos réponses sont **anonymes**.")
                pa.set_opts({"questions": HOT, "anonymous": True, "show_results": False})
                db.session.add(pa)
                db.session.flush()
                for e in enrs:
                    if rnd.random() < .7:
                        ans = {"0": str(rnd.choice([4, 4, 5, 5, 3])), "1": str(rnd.choice([4, 5, 5, 3])), "2": str(rnd.choice([5, 5, 4])),
                               "3": rnd.choice(["Adapté", "Adapté", "Adapté", "Trop rapide"]), "4": rnd.choice(TEXTS),
                               "5": rnd.choice(["Oui", "Oui", "Oui", "Je ne sais pas"])}
                        db.session.add(PollResponse(activity_id=pa.id, enrollment_id=e.id, answers=json.dumps(ans, ensure_ascii=False)))
                        db.session.add(ActivityCompletion(activity_id=pa.id, enrollment_id=e.id, viewed_at=now, completed_at=now))
        db.session.commit()
        return 1
    finally:
        g.school_id, g.unscoped = prev


if __name__ == "__main__":
    import os
    os.environ.setdefault("DISABLE_SCHEDULER", "1")
    from app import app
    with app.app_context():
        print("communauté :", add())
