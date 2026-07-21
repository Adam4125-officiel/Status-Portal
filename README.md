# status-portal

Portail de statut perso pour ton serveur (Jellyfin, SMB, etc.) : liens, annonces,
incidents/maintenances, infos pratiques — tout modifiable depuis un panel admin,
sans jamais toucher au HTML.

- Backend : **Python / Flask** (pas besoin de connaître Flask, tu géreras tout via le navigateur)
- Stockage : **SQLite** (un seul fichier, `instance/portal.db`, créé automatiquement)
- Auto-refresh de la page publique toutes les 60s
- Health check automatique optionnel (ping HTTP périodique par service)

---

## 1. Installation

Il te faut Python 3.10+ sur la machine qui héberge ça (ton serveur ou la
machine IIS — peu importe tant qu'IIS peut l'atteindre en réseau).

```powershell
cd status-portal
pip install -r requirements.txt
```

## 2. Premier lancement (test en local)

```powershell
python app.py
```

Ouvre `http://localhost:5000` → page publique.
Ouvre `http://localhost:5000/admin` → ça te demande de créer le mot de passe
admin au premier lancement (aucun mot de passe par défaut, c'est toi qui le
choisis). Note-le, y'a pas de "mot de passe oublié".

Une fois que t'as vu que ça marche, `Ctrl+C` pour arrêter — `app.py` est le
serveur de **dev**, pas fait pour tourner 24/7.

## 3. Lancement en continu (production)

Utilise `serve_waitress.py` à la place de `app.py` — c'est un vrai serveur
WSGI (waitress), pas le serveur de dev de Flask.

```powershell
python serve_waitress.py
```

Ça écoute sur le port 5000 par défaut (change avec la variable d'env `PORTAL_PORT`).

### Le faire tourner au démarrage de Windows

Le plus simple sans installer d'outil tiers : **Planificateur de tâches Windows**.

1. Ouvre le Planificateur de tâches → "Créer une tâche"
2. Onglet General : coche "Exécuter que l'utilisateur soit connecté ou non"
3. Onglet Déclencheurs : "Au démarrage de l'ordinateur"
4. Onglet Actions : "Démarrer un programme"
   - Programme : `C:\chemin\vers\python.exe`
   - Arguments : `serve_waitress.py`
   - Dossier de démarrage : `C:\chemin\vers\status-portal`
5. Enregistre.

(Alternative si tu veux un vrai service Windows avec logs propres : `nssm`
— *Non-Sucking Service Manager*, un exe portable, pas besoin d'install.)

### ⚠️ Important : la clé de session

Sans ça, chaque redémarrage du serveur te déconnecte de l'admin. Définis une
variable d'environnement système fixe :

```powershell
[Environment]::SetEnvironmentVariable("PORTAL_SECRET_KEY", "une-longue-chaine-aleatoire-a-toi", "Machine")
```

(Génère une chaîne aléatoire une fois, mets-la là, oublie-la après.)

## 4. Exposer ça sur ton domaine via IIS

IIS ne fait pas tourner Python nativement — il faut le faire agir en
**reverse proxy** vers `http://localhost:5000` (là où tourne `serve_waitress.py`).

1. Installe deux modules IIS (une fois) :
   - **URL Rewrite** : https://www.iis.net/downloads/microsoft/url-rewrite
   - **Application Request Routing (ARR)** : https://www.iis.net/downloads/microsoft/application-request-routing
2. Dans IIS Manager → sélectionne le serveur (racine, pas un site) → double-clic
   "Application Request Routing Cache" → dans le panneau de droite, "Server
   Proxy Settings" → coche **Enable proxy** → Appliquer.
3. Crée ton site IIS normalement (binding sur ton domaine, port 80/443,
   certif SSL si t'en as un — recommandé).
4. Dans ce site → double-clic **URL Rewrite** → "Add Rule(s)" → **Reverse Proxy**
   → entre `localhost:5000` comme serveur cible.
5. Sauvegarde. IIS reçoit les requêtes sur ton domaine et les relaie en
   coulisses vers Flask/waitress.

Ton routeur/box doit rediriger les ports 80/443 vers la machine IIS, comme
d'hab pour exposer un service chez toi.

## 5. Utilisation au quotidien

Tout se passe dans `/admin` :

- **Services** : ajoute/modifie tes services (Jellyfin, SMB...), avec lien de
  redirection, icône, statut. Active "auto-check" + donne une URL de
  vérification si le service a un endpoint qui répond en HTTP 200 — sinon
  laisse en statut manuel et change-le toi-même.
- **Annonces** : bandeau en haut du portail public. `**gras**`, liens auto-cliquables.
- **Incidents** : historique de pannes/maintenances, avec statut
  (investigation → identifié → sous surveillance → résolu).
- **Page infos** : texte libre en bas de page (accès SMB, VPN, contact...).
- **Réglages** : changer le mot de passe admin.

Rien à recompiler, rien à re-déployer : chaque modif est en base et visible
immédiatement (ou dans les 60s max, le temps de l'auto-refresh de la page).

## 6. Structure du projet

```
status-portal/
  app.py                  # routes Flask (public + admin)
  serve_waitress.py       # à lancer en prod (au lieu de app.py)
  db.py                   # toute la couche SQLite
  requirements.txt
  instance/portal.db      # créé automatiquement au premier lancement
  templates/               # pages HTML (Jinja2)
  static/css/style.css     # tout le design
  static/js/main.js        # auto-refresh de la page publique
```

Pour changer la fréquence du health check auto : `CHECK_INTERVAL_SECONDS`
tout en haut de `app.py` (120s par défaut).
