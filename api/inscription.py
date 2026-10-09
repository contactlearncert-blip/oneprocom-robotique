"""
api/inscription.py
Fonction serverless Vercel (runtime Python) qui traite le formulaire d'inscription.

Version 5 : ID unique + QR code (Nom Prénom, Profession, ID) dans le mail,
  stockage des inscrits dans Upstash Redis (gratuit) pour l'espace admin.

Version 4 :
  - L'inscrit reçoit un mail reprenant les modalités de paiement + son QR code en PDF
    (pièce jointe).
  - L'inscrit est aussi redirigé vers la page de confirmation (confirmation.html),
    qui affiche le même contenu, pour une action immédiate.
  - L'agence (ADMIN_EMAIL) reçoit un mail de notification structuré, pour
    report dans le fichier Excel.

Route exposée automatiquement par Vercel : POST /api/inscription

Variables d'environnement à définir dans Vercel (Project Settings > Environment Variables) :
    GMAIL_ADRESSE       -> oneprocom.robotique@gmail.com
    GMAIL_MOT_DE_PASSE  -> mot de passe d'application Gmail (16 caractères)
    ADMIN_EMAIL         -> adresse qui reçoit les notifications (par défaut = GMAIL_ADRESSE)
    KV_REST_API_URL / KV_REST_API_TOKEN -> ajoutées automatiquement par Vercel
                           quand on connecte une base Upstash Redis (plan Free)
"""

import io
import json
import os
import re
import secrets
import smtplib
import ssl
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlencode

import segno
from PIL import Image, ImageDraw, ImageFont

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Stockage des inscrits (Upstash Redis, offre gratuite, via l'API REST)
# ---------------------------------------------------------------------------
# Sur Vercel : Storage > Marketplace > Upstash Redis (plan Free). Vercel
# injecte alors automatiquement les variables d'environnement
# (KV_REST_API_URL / KV_REST_API_TOKEN, ou UPSTASH_REDIS_REST_*).

CLE_INSCRITS = "oneprocom:inscrits"
CLE_COMPTEUR = "oneprocom:compteur"


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

def _redis_config():
    url, token = _env_redis()
    return (url.rstrip("/"), token) if url and token else (None, None)


def redis(*commande):
    """Exécute une commande Redis via REST. Lève une exception si non configuré."""
    url, token = _redis_config()
    if not url:
        raise RuntimeError("Base Upstash Redis non connectée (variables KV_REST_API_* absentes).")
    req = urllib.request.Request(
        url,
        data=json.dumps(list(commande)).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


def generer_id():
    """ID lisible et unique : OPC-0001, OPC-0002... (compteur atomique Redis).
    Si la base est indisponible, repli sur un ID aléatoire pour ne jamais bloquer l'inscrit."""
    try:
        return f"OPC-{int(redis('INCR', CLE_COMPTEUR)):04d}"
    except Exception as e:
        print("Compteur indisponible, ID aléatoire :", repr(e))
        return "OPC-" + secrets.token_hex(3).upper()


def enregistrer_inscrit(inscrit):
    redis("HSET", CLE_INSCRITS, inscrit["id"], json.dumps(inscrit, ensure_ascii=False))


def contenu_qr(id_inscrit, nom, prenom, profession):
    """Texte encodé dans le QR code : Nom et Prénom, Profession, ID."""
    return (
        f"Nom et Prénom : {prenom} {nom}\n"
        f"Profession : {profession}\n"
        f"ID : {id_inscrit}"
    )


DOSSIER_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")


def generer_pdf_qr(texte_qr, prenom, nom):
    """PDF classique de l'inscrit : QR code et « SCAN ME » en dessous,
    sans aucun autre texte. Retourne les octets d'un PDF (A5 portrait)."""
    L, H = 874, 1240  # A5 à 150 dpi
    bleu = (11, 58, 99)
    page = Image.new("RGB", (L, H), (255, 255, 255))
    d = ImageDraw.Draw(page)
    chemin_police = os.path.join(DOSSIER_ASSETS, "DejaVuSans-Bold.ttf")

    def police(taille):
        return ImageFont.truetype(chemin_police, taille)

    def centre(texte, y, taille, couleur=bleu):
        f = police(taille)
        d.text(((L - d.textlength(texte, font=f)) / 2, y), texte, font=f, fill=couleur)

    # --- QR code, à module entier pour rester net et facile à scanner
    qr_obj = segno.make(texte_qr, error="m", encoding="utf-8")
    modules, _ = qr_obj.symbol_size(scale=1, border=1)
    echelle = max(4, 520 // modules)
    buf = io.BytesIO()
    qr_obj.save(buf, kind="png", scale=echelle, border=1)
    qr = Image.open(buf).convert("RGB")
    t = qr.size[0]
    hauteur_bloc = t + 25 + 46 + 12  # QR + « SCAN ME »
    y_qr = (H - hauteur_bloc) // 2
    page.paste(qr, ((L - t) // 2, y_qr))

    # --- « SCAN ME » sous le QR
    centre("SCAN ME", y_qr + t + 25, 46)


    sortie = io.BytesIO()
    page.save(sortie, format="PDF", resolution=150.0)
    return sortie.getvalue()


def construire_mail_inscrit(id_inscrit, nom, prenom, email, telephone, profession):
    """
    Mail envoyé à l'inscrit : confirmation + QR code (Nom et Prénom,
    Profession, ID) affiché dans le mail et en pièce jointe PDF.
    """
    gmail_adresse = os.environ.get("GMAIL_ADRESSE", "oneprocom.robotique@gmail.com")

    nom_affiche = f"{nom.upper()} {prenom}".strip()

    # QR code en PNG, affiché directement dans le corps du mail
    qr_obj = segno.make(contenu_qr(id_inscrit, nom, prenom, profession), error="m", encoding="utf-8")
    buf = io.BytesIO()
    qr_obj.save(buf, kind="png", scale=8, border=2)
    qr_png = buf.getvalue()

    msg = EmailMessage()
    msg["Subject"] = "One Pro Com"
    msg["From"] = f"One Pro Com <{gmail_adresse}>"
    msg["To"] = email

    # Version texte brut (fallback si le client mail n'affiche pas le HTML)
    msg.set_content(
        f"Bonjour {nom_affiche},\n\n"
        "Votre inscription à la formation organisée par One Pro Com "
        "en collaboration avec L Partners a bien été enregistrée.\n\n"
        "Votre QR Code est joint à cet e-mail.\n\n"
        "Merci de le présenter le jour de la formation.\n\n"
        "Nous vous remercions de votre confiance.\n\n"
        "One Pro Com\n"
        "L Partners"
    )

    corps_html = f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#ffffff;font-family:Arial,Helvetica,sans-serif;color:#1f2d3a;">
  <div style="max-width:520px;padding:24px 20px;">
    <h2 style="margin:0 0 20px;font-size:22px;color:#111;">Bonjour {escape(nom_affiche)},</h2>
    <p style="margin:0 0 14px;font-size:15px;line-height:1.5;">Votre inscription à la formation organisée par <strong>One Pro Com</strong> en collaboration avec <strong>L Partners</strong> a bien été enregistrée.</p>
    <p style="margin:0 0 14px;font-size:15px;line-height:1.5;">Votre QR Code est joint à cet e-mail.</p>
    <p style="margin:0 0 28px;font-size:15px;line-height:1.5;">Merci de le présenter le jour de la formation.</p>
    <p style="margin:0 0 24px;font-size:15px;line-height:1.5;">Nous vous remercions de votre confiance.</p>
    <p style="margin:0 0 20px;font-size:15px;line-height:1.4;"><strong>One Pro Com</strong><br><span style="color:#888;font-weight:bold;">L Partners</span></p>
    <img src="cid:qrcode" alt="QR Code" width="220" style="display:block;width:220px;max-width:100%;height:auto;">
  </div>
</body>
</html>
"""
    msg.add_alternative(corps_html, subtype="html")
    msg.get_payload()[1].add_related(qr_png, maintype="image", subtype="png", cid="<qrcode>")

    # QR code aussi en pièce jointe PDF (à imprimer / présenter)
    pdf_qr = generer_pdf_qr(contenu_qr(id_inscrit, nom, prenom, profession), prenom, nom)
    msg.add_attachment(pdf_qr, maintype="application", subtype="pdf", filename="qr-code.pdf")

    return msg


def envoyer_message(msg):
    gmail_adresse = os.environ.get("GMAIL_ADRESSE", "oneprocom.robotique@gmail.com")
    gmail_mdp = os.environ.get("GMAIL_MOT_DE_PASSE")
    if not gmail_mdp:
        raise RuntimeError(
            "Variable d'environnement GMAIL_MOT_DE_PASSE manquante. "
            "Configurez-la dans Vercel (Project Settings > Environment Variables)."
        )
    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(gmail_adresse, gmail_mdp)
        server.send_message(msg)


def envoyer_mail_admin(id_inscrit, nom, prenom, email, telephone, profession):
    """Mail envoyé à l'agence : données structurées pour report dans le fichier Excel."""
    gmail_adresse = os.environ.get("GMAIL_ADRESSE", "oneprocom.robotique@gmail.com")
    gmail_mdp = os.environ.get("GMAIL_MOT_DE_PASSE")
    admin_email = os.environ.get("ADMIN_EMAIL", gmail_adresse)

    if not gmail_mdp:
        raise RuntimeError(
            "Variable d'environnement GMAIL_MOT_DE_PASSE manquante. "
            "Configurez-la dans Vercel (Project Settings > Environment Variables)."
        )

    if not admin_email:
        return  # notification admin désactivée

    msg = EmailMessage()
    msg["Subject"] = f"Nouvelle inscription : {prenom} {nom} ({id_inscrit})"
    msg["From"] = gmail_adresse
    msg["To"] = admin_email
    msg["Reply-To"] = email

    msg.set_content(
        "Nouvelle inscription (consultable aussi dans l'espace admin /admin.html) :\n\n"
        f"ID : {id_inscrit}\n"
        f"Nom : {nom}\n"
        f"Prénom : {prenom}\n"
        f"Email : {email}\n"
        f"Téléphone : {telephone}\n"
        f"Profession : {profession}\n"
    )
    msg.add_alternative(
        f"""
        <h2>Nouvelle inscription (consultable aussi dans l'espace admin /admin.html)</h2>
        <table cellpadding="6" cellspacing="0" border="1" style="border-collapse:collapse;">
            <tr><td><strong>ID</strong></td><td>{escape(id_inscrit)}</td></tr>
            <tr><td><strong>Nom</strong></td><td>{escape(nom)}</td></tr>
            <tr><td><strong>Prénom</strong></td><td>{escape(prenom)}</td></tr>
            <tr><td><strong>Email</strong></td><td>{escape(email)}</td></tr>
            <tr><td><strong>Téléphone</strong></td><td>{escape(telephone)}</td></tr>
            <tr><td><strong>Profession</strong></td><td>{escape(profession)}</td></tr>
        </table>
        """,
        subtype="html",
    )
    envoyer_message(msg)


def page_html(titre, corps, code=200):
    return code, f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"><title>{titre}</title></head>
<body>
{corps}
</body>
</html>"""


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        champs = parse_qs(raw_body)

        nom = champs.get("nom", [""])[0].strip()
        prenom = champs.get("prenom", [""])[0].strip()
        email = champs.get("email", [""])[0].strip()
        telephone = champs.get("telephone", [""])[0].strip()
        profession = champs.get("profession", [""])[0].strip()

        if not all([nom, prenom, email, telephone, profession]):
            code, html = page_html(
                "Erreur - Inscription",
                "<p>Tous les champs sont obligatoires.</p>"
                "<p><a href=\"javascript:history.back()\">Retour au formulaire</a></p>",
                400,
            )
            self._repondre_html(code, html)
            return

        if not EMAIL_REGEX.match(email):
            code, html = page_html(
                "Erreur - Inscription",
                "<p>L'adresse email fournie n'est pas valide.</p>"
                "<p><a href=\"javascript:history.back()\">Retour au formulaire</a></p>",
                400,
            )
            self._repondre_html(code, html)
            return

        # 1) ID unique + enregistrement (espace admin). Un échec de stockage
        #    ne bloque jamais l'inscrit : on logue et on continue.
        id_inscrit = generer_id()
        inscrit = {
            "id": id_inscrit,
            "nom": nom,
            "prenom": prenom,
            "email": email,
            "telephone": telephone,
            "profession": profession,
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            enregistrer_inscrit(inscrit)
        except Exception as e:
            print("Erreur enregistrement inscrit :", repr(e))

        # 2) Mail à l'inscrit (avec son QR code en PDF) et notification à l'agence.
        try:
            envoyer_message(construire_mail_inscrit(id_inscrit, nom, prenom, email, telephone, profession))
        except Exception as e:
            print("Erreur envoi mail inscrit :", repr(e))

        try:
            envoyer_mail_admin(id_inscrit, nom, prenom, email, telephone, profession)
        except Exception as e:
            print("Erreur envoi mail admin (inscription) :", repr(e))

        # 3) Redirection (303) vers la page de confirmation.
        params = urlencode({"prenom": prenom, "nom": nom})
        self.send_response(303)
        self.send_header("Location", f"/confirmation.html?{params}")
        self.end_headers()

    def _repondre_html(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))
