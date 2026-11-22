"""Classes virtuelles et badges de démonstration (idempotent)."""
from datetime import datetime, timedelta

from models import Activity, Course, CourseAward, School, db


def add(app_globals, slug="institut-prolearning-demo"):
    from flask import g
    from tenancy import unscoped
    with unscoped():
        sc = School.query.filter_by(slug=slug).first()
        if not sc or Activity.query.filter_by(school_id=sc.id, kind="live").first():
            return 0
    prev = g.get("school_id"), g.get("unscoped", False)
    g.school_id, g.unscoped = sc.id, False
    try:
        A = app_globals
        for prefix, badge, delta, hour in (("Lean Six Sigma Green Belt", "Green Belt — DMAIC", 1, 18),
                                           ("Inspecteur de soudage", "Inspecteur de soudage — e-learning", 3, 17),
                                           ("Maths 4AM", "Champion BEM", 2, 19)):
            c = Course.query.filter(Course.title.like(prefix + "%")).first()
            if not c:
                continue
            o = c.opts()
            o.update({"badge_name": badge, "min_grade": 10 if not prefix.startswith("Maths") else None})
            c.set_opts(o)
            sec = c.sections.first()
            a = Activity(course_id=c.id, section_id=sec.id, position=sec.activities.count(), kind="live", completion="auto",
                         title="Classe virtuelle — questions / réponses", url="https://meet.google.com/",
                         body="Séance en direct avec le formateur pour répondre à vos questions. Remplacez le lien par celui de votre réunion.")
            start = (datetime.now() + timedelta(days=delta)).replace(hour=hour, minute=0, second=0, microsecond=0)
            f = {"start": start.strftime("%Y-%m-%dT%H:%M"), "duration": "60", "link_attendance": "1", "completion": "none"}

            class F(dict):
                def get(self, k, d=None):
                    return dict.get(self, k, d)
            db.session.add(a)
            db.session.flush()
            A["save_activity_opts"](a, F(f))
            from models import ActivityCompletion
            enrs = sorted(c.formation.enrollments.filter_by(status="confirmed").all(),
                          key=lambda e: -ActivityCompletion.query.filter_by(enrollment_id=e.id).count())
            tracked = [x for x in c.activities(True) if x.kind != "label" and x.completion != "none" and x.kind not in ("quiz", "assign")]
            for e in enrs[:3]:          # the most advanced learners finish the course
                for x in tracked:
                    r = ActivityCompletion.query.filter_by(activity_id=x.id, enrollment_id=e.id).first()
                    if not r:
                        r = ActivityCompletion(activity_id=x.id, enrollment_id=e.id, viewed_at=datetime.utcnow())
                        db.session.add(r)
                    r.completed_at = r.completed_at or datetime.utcnow()
            db.session.flush()
            for e in enrs:
                A["check_award"](c, e)
        db.session.commit()
        return 1
    finally:
        g.school_id, g.unscoped = prev
