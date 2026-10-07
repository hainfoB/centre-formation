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

## Nouveautés (révision)
- Recherche globale (touche « / »), compteurs d'impayés et d'inscriptions en attente dans le menu.
- Page **Impayés & relances** : toutes formations, filtre, relance WhatsApp tracée (date, nombre), relance « à la suite ».
- Alertes **absences répétées** au tableau de bord + message WhatsApp au parent.
- Tableau de bord : encaissements sur 6 mois, échéances à 7 jours, formations en cours, guide de démarrage.
- Formations : nouvelle session par duplication (planning décalé), suppression sécurisée, attestations en lot (PDF).
- Feuille d'émargement imprimable (avec pointages ou vierge).
- Logo du centre (menu, pages publiques, reçus, attestations, émargement), sauvegarde complète Excel, export des stagiaires.
- Sécurité : HTTPS derrière proxy, cookies sécurisés, en-têtes de sécurité, anti-force brute à la connexion, limites sur les formulaires publics, anti double-clic, redirections sûres, migration automatique des colonnes.

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
