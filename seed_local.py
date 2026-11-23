"""Première ouverture de la version locale : crée l'école de démonstration complète
(entreprises, formations, cours en ligne, quiz, forums, badges). Ne supprime jamais rien."""
import os

os.environ.setdefault("DISABLE_SCHEDULER", "1")
from app import app, today  # noqa: E402

with app.app_context():
    import seed_assess
    import seed_community
    import seed_courses
    import seed_extras
    import seed_pro
    import app as A
    made = seed_pro.run(today())
    seed_courses.add()
    seed_assess.add()
    seed_community.add()
    seed_extras.add(vars(A))
    print("Ecole de demonstration creee." if made else "La base existe deja.")
    print("Connexion : direction@prolearning-demo.dz / demo1234")
