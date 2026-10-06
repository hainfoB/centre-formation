# Centre de formation — gestion

Application Flask pour un centre de formation privé : formations, inscriptions en ligne, séances, présences par QR code, paiements échelonnés avec relances, caisse, attestations PDF vérifiables et espace parents.

## Fonctions
- **Formations** : gratuites ou payantes (frais d'inscription + mensualités), places limitées, liste d'attente automatique, lien d'inscription public.
- **Stagiaires** : fiche complète, contact parent/tuteur, recherche, historique.
- **Séances** : génération du planning par jours de la semaine.
- **Présences** : A) l'accueil scanne le QR personnel du stagiaire, B) QR projeté en salle (change toutes les 45 s), C) code à 4 chiffres pour l'en ligne ; entrée/sortie, retards, saisie manuelle.
- **Paiements** : échéancier automatique, paiement partiel (reliquat), remise, exonération, reçu imprimable, relances email J-3/J/J+3/J+10, bouton WhatsApp stagiaire et parent.
- **Caisse** : encaissements par période et par mode, export Excel.
- **Attestations** : PDF avec QR de vérification, selon le seuil d'assiduité (et le paiement si exigé).
- **Espace parents** : lien privé (présences, absences, paiements, attestations).
- **Rôles** : administrateur, secrétariat, formateur (présences de ses formations seulement).

## Déploiement sur Railway
1. Créer un projet, ajouter **PostgreSQL**, puis un service depuis ce dépôt GitHub.
2. Variables du service :
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `SECRET_KEY` = une longue chaîne aléatoire
   - `APP_URL` = l'URL publique (ex. `https://centre.up.railway.app`)
   - emails (facultatif) : `BREVO_API_KEY`, `EMAIL_FROM`
3. Ouvrir l'URL : la première visite crée le compte administrateur.

## En local
```
pip install -r requirements.txt
python app.py   # http://localhost:5000 (SQLite)
```
