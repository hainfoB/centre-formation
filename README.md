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

## Tester sur votre ordinateur (localhost)
**Windows** : double-cliquez sur `run_local.bat`. La première fois, le script installe Python dans un dossier `.venv`
et crée une base de démonstration remplie (8 formations, 125 élèves, séances, présences, paiements, charges sur 12 mois).
Le navigateur s'ouvre sur http://localhost:5000.

Comptes de démonstration (mot de passe `demo1234`) :
- `admin@demo.dz` : direction (tout, dont bénéfices et paie)
- `secretariat@demo.dz` : secrétariat
- `amina@demo.dz` : formatrice (présences de ses séances)

`reset_demo.bat` efface la base locale et recrée les données d'exemple. Linux / macOS : `./run_local.sh`.
Prérequis : Python 3.10 ou plus récent (python.org, cocher « Add Python to PATH »).
La base locale (`instance/centre.db`) n'est jamais envoyée sur GitHub ni sur Railway ; `seed_demo.py` refuse de
s'exécuter sur la base PostgreSQL de production.

## Langues
Interface en français, arabe (de droite à gauche) et anglais : sélecteur en bas du menu et en haut des pages publiques.
La langue est mémorisée par utilisateur ; la langue par défaut se règle dans Paramètres. Traductions dans `translations/`.

## Finances
- **Caisse** : journal de tous les mouvements (frais encaissés, dépenses, salaires, autres recettes), solde espèces
  et solde CCP / banque (à partir des soldes initiaux saisis dans Paramètres), export Excel.
- **Paie des enseignants** : tarif à l'heure, à la séance ou fixe mensuel ; montant dû calculé à partir des séances tenues ;
  paiement enregistré dans la caisse.
- **Bénéfices** : recettes, charges, bénéfice et marge par mois, trimestre et année, comparaison avec la période précédente,
  graphique mensuel, détail par formation et par catégorie de charge, export Excel.

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

## Plateforme multi-écoles & licences

- **Créer une école** : `/creer-mon-ecole` → 30 jours d'essai, toutes fonctions. Chaque école a ses propres
  élèves, paiements, caisse, paramètres et utilisateurs (isolation automatique, `tenancy.py`).
- **Offres** : Essai 30 j (gratuit) · Pass+ 1 an (8 000 DA) · Ultimate 2 ans (14 000 DA) ·
  À vie (60 000 DA : version locale hors ligne + réseau local, 6 mois de service client).
- **Panneau plateforme** `/plateforme` (propriétaire uniquement) : écoles, expirations, suspension, génération de clés.
  - clé courte `HEDU-XXXX-XXXX-XXXX` : version en ligne, usage unique ;
  - clé signée `HEDUL-…` : vérifiable sans Internet (installation locale), liée au nom de l'école si renseigné.
  - variable Railway requise pour signer : `LICENSE_PRIVATE_KEY`. `SALES_PHONE` = WhatsApp de renouvellement.
- **Expiration** : l'école passe en lecture seule (données consultables, ajout/modification bloqués) ; bandeau à J-15.
- **Transfert local ⇄ en ligne** : Abonnement & données → export `.hedu` puis import (remplace les données de l'école).
- **Version locale** : `run_local.bat` (APP_MODE=local) écoute sur le réseau de l'école : l'adresse
  `http://IP-du-PC:5000` s'affiche au démarrage pour les téléphones et PC du même Wi-Fi.
- La base existante (mono-école) est migrée automatiquement vers l'école n°1 au premier démarrage ;
  le premier administrateur devient propriétaire de la plateforme.
