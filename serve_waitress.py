"""
serve_waitress.py — Point d'entrée PRODUCTION.
Utilise waitress (serveur WSGI correct pour Windows) au lieu du serveur de dev Flask.

Lancement :
    python serve_waitress.py

C'est CE script qu'il faut lancer au démarrage de Windows (via tâche planifiée
ou service, voir README.md), pas app.py.
"""
import os
from waitress import serve

import db
from app import app, start_background_checker

PORT = int(os.environ.get("PORTAL_PORT", "5000"))

if __name__ == "__main__":
    db.init_db()
    start_background_checker()
    print(f"status-portal démarré sur http://0.0.0.0:{PORT}")
    serve(app, host="0.0.0.0", port=PORT)
