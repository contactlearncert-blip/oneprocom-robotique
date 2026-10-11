"""
api/admin.py
Liste des inscrits pour l'espace admin (/admin.html).

GET /api/admin   avec l'en-tête  X-Admin-Password: <mot de passe>

Variables d'environnement (Vercel) :
    ADMIN_PASSWORD                         -> mot de passe de l'espace admin
    KV_REST_API_URL / KV_REST_API_TOKEN    -> fournies par Upstash Redis (plan Free)
"""

import hmac
import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

CLE_INSCRITS = "oneprocom:inscrits"


def _env_redis():
    """Trouve l'URL/token REST Upstash, quel que soit le préfixe choisi dans Vercel
    (KV_REST_API_URL, UPSTASH_REDIS_REST_URL, STORAGE_KV_REST_API_URL, ...)."""
    url = token = None
    for nom, val in os.environ.items():
        if not val:
            continue
        if nom.endswith("REST_API_URL") or nom.endswith("REDIS_REST_URL"):
            url = url or val
        elif nom.endswith("REST_API_TOKEN") or nom.endswith("REDIS_REST_TOKEN"):
            if "READ_ONLY" not in nom:
                token = token or val
    return url, token

def redis(*commande):
    url, token = _env_redis()
    if not url or not token:
        raise RuntimeError("Base Upstash Redis non connectée.")
    req = urllib.request.Request(
        url.rstrip("/"),
        data=json.dumps(list(commande)).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        mot_de_passe = os.environ.get("ADMIN_PASSWORD", "")
        fourni = self.headers.get("X-Admin-Password", "")

        if not mot_de_passe:
            return self._json(500, {"erreur": "ADMIN_PASSWORD n'est pas défini dans Vercel."})

        if not hmac.compare_digest(fourni.encode("utf-8"), mot_de_passe.encode("utf-8")):
            time.sleep(1)  # freine les essais à répétition
            return self._json(401, {"erreur": "Mot de passe incorrect."})

        try:
            plat = redis("HGETALL", CLE_INSCRITS) or []
        except Exception as e:
            print("Erreur lecture inscrits :", repr(e))
            return self._json(500, {"erreur": "Base de données inaccessible. Vérifiez la connexion Upstash Redis."})

        # HGETALL renvoie [champ1, valeur1, champ2, valeur2, ...]
        inscrits = []
        for i in range(1, len(plat), 2):
            try:
                inscrits.append(json.loads(plat[i]))
            except ValueError:
                continue
        inscrits.sort(key=lambda x: x.get("date", ""), reverse=True)
        self._json(200, {"total": len(inscrits), "inscrits": inscrits})

    def _json(self, code, data):
        corps = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corps)
