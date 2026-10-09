"""
api/inscription.py
Fonction serverless Vercel (runtime Python) qui traite le formulaire d'inscription.

Version 5 : ID unique + QR code (Nom Prénom, Profession, ID) dans le mail,
  stockage des inscrits dans Upstash Redis (gratuit) pour l'espace admin.

Version 4 :
  - L'inscrit reçoit un mail reprenant les modalités de paiement + un bouton
    WhatsApp stylé, pour confirmer son inscription.
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
from email.utils import make_msgid
from html import escape
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlencode, quote

import segno
from PIL import Image, ImageDraw, ImageFilter, ImageFont

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WHATSAPP_NUMERO = "212716639486"  # format international, sans +, sans espaces


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


def generer_badge(texte_qr, prenom, nom):
    """Badge de l'inscrit : l'affiche de l'atelier avec son QR code posé dessus
    et son nom en clair (aucun ID visible). Retourne les octets d'un JPEG."""
    affiche = Image.open(os.path.join(DOSSIER_ASSETS, "affiche.jpg")).convert("RGB")
    L, H = affiche.size
    k = L / 822.0  # coordonnées calées sur une affiche de 822 px de large

    # --- QR code, à module entier pour rester net et facile à scanner
    qr_obj = segno.make(texte_qr, error="m", encoding="utf-8")
    modules, _ = qr_obj.symbol_size(scale=1, border=1)
    echelle = max(4, int(235 * k) // modules)
    buf = io.BytesIO()
    qr_obj.save(buf, kind="png", scale=echelle, border=1)
    qr = Image.open(buf).convert("RGB")
    t = qr.size[0]

    # --- Nom en clair sous le QR (une ligne, sinon prénom / nom sur deux lignes)
    marge = int(14 * k)
    largeur_carte = t + 2 * marge
    chemin_police = os.path.join(DOSSIER_ASSETS, "DejaVuSans-Bold.ttf")
    dessin_mesure = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def police(taille):
        return ImageFont.truetype(chemin_police, taille)

    def largeur(texte, taille):
        return dessin_mesure.textlength(texte, font=police(taille))

    nom_complet = f"{prenom} {nom}".strip()
    max_l = largeur_carte - 2 * int(10 * k)
    taille = int(30 * k)
    while taille > int(18 * k) and largeur(nom_complet, taille) > max_l:
        taille -= 1
    if largeur(nom_complet, taille) <= max_l:
        lignes = [nom_complet]
    else:
        taille = int(24 * k)
        lignes = [prenom, nom]
        while taille > 12 and max(largeur(x, taille) for x in lignes) > max_l:
            taille -= 1
    interligne = int(taille * 1.25)
    hauteur_nom = interligne * len(lignes)

    hauteur_carte = marge + t + int(10 * k) + hauteur_nom + marge
    x0, y0 = int(95 * k), int(322 * k)

    # --- Ombre douce + carte blanche aux coins arrondis
    calque = Image.new("RGBA", affiche.size, (0, 0, 0, 0))
    ImageDraw.Draw(calque).rounded_rectangle(
        [x0 + 3, y0 + 6, x0 + largeur_carte + 3, y0 + hauteur_carte + 6],
        radius=int(14 * k), fill=(11, 58, 99, 90))
    calque = calque.filter(ImageFilter.GaussianBlur(int(9 * k)))
    badge = Image.alpha_composite(affiche.convert("RGBA"), calque)
    d = ImageDraw.Draw(badge)
    d.rounded_rectangle([x0, y0, x0 + largeur_carte, y0 + hauteur_carte],
                        radius=int(14 * k), fill=(255, 255, 255, 255))
    badge.paste(qr, (x0 + marge, y0 + marge))

    y = y0 + marge + t + int(10 * k)
    f = police(taille)
    for ligne in lignes:
        l = d.textlength(ligne, font=f)
        d.text((x0 + (largeur_carte - l) / 2, y), ligne, font=f, fill=(11, 58, 99, 255))
        y += interligne

    sortie = io.BytesIO()
    badge.convert("RGB").save(sortie, format="JPEG", quality=90, optimize=True)
    return sortie.getvalue()


def construire_mail_inscrit(id_inscrit, nom, prenom, email, telephone, profession):
    """
    Mail envoyé à l'inscrit : badge (affiche + QR code Nom et Prénom, Profession, ID) +
    modalités de paiement + bouton WhatsApp.

    Note technique : les clients mail ne supportent pas la balise <button>
    ni JavaScript. Le "bouton" est donc un lien <a> stylé en CSS inline
    (fond coloré, padding, coins arrondis) pour ressembler à un bouton.
    Fonctionne bien sur Gmail, Apple Mail, Outlook.com et mobile ; sur
    Outlook Bureau (Windows), les coins arrondis peuvent être ignorés mais
    le bouton reste cliquable normalement.
    """
    gmail_adresse = os.environ.get("GMAIL_ADRESSE", "oneprocom.robotique@gmail.com")

    nom_complet = f"{prenom} {nom}".strip()
    badge_jpg = generer_badge(contenu_qr(id_inscrit, nom, prenom, profession), prenom, nom)
    badge_cid = make_msgid(domain="oneprocom.local")
    wa_cid = make_msgid(domain="oneprocom.local")
    with open(os.path.join(DOSSIER_ASSETS, "whatsapp.png"), "rb") as f:
        wa_png = f.read()
    message_whatsapp = (
        f"Bonjour, je viens de m’inscrire à la formation One Pro Com ({nom_complet}). "
        "J’aimerais échanger avec vous."
    )
    lien_whatsapp = f"https://wa.me/{WHATSAPP_NUMERO}?text={quote(message_whatsapp)}"

    msg = EmailMessage()
    msg["Subject"] = "Inscription bien reçue — votre badge"
    msg["From"] = gmail_adresse
    msg["To"] = email

    # Version texte brut (fallback si le client mail n'affiche pas le HTML)
    msg.set_content(
        f"Bonjour {prenom},\n\n"
        "Inscription bien reçue !\n\n"
        "Votre badge avec votre QR code est joint à ce mail (image badge.jpg). "
        "Conservez-le : il vous sera demandé à l'entrée.\n\n"
        "Modalités de paiement — vous avez deux possibilités :\n\n"
        "1. Paiement avant la formation : vous pouvez effectuer le paiement "
        "à l'avance afin de confirmer votre inscription.\n\n"
        "2. Paiement sur place : vous pouvez également régler sur place, "
        "avant le début de la séance.\n\n"
        "Une question ? Contactez-nous sur WhatsApp :\n"
        f"{lien_whatsapp}\n\n"
        "L'équipe One Pro Com"
    )

    # Version HTML avec le bouton, reprenant le contenu de confirmation.html
    msg.add_alternative(
        f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#eef5ff;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef5ff;padding:30px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width:480px;background:#ffffff;border-radius:16px;overflow:hidden;">
          <tr>
            <td style="padding:34px 30px;">
              <h1 style="margin:0 0 12px;color:#0b3a63;font-size:22px;text-align:center;">Inscription bien reçue !</h1>
              <img src="cid:{badge_cid[1:-1]}" alt="Votre badge" width="420" style="display:block;width:100%;max-width:420px;height:auto;margin:18px auto 8px;border-radius:12px;">
              <p style="margin:0 0 26px;color:#8aa2b8;font-size:12px;text-align:center;">Conservez ce badge : il vous sera demandé à l'entrée.</p>

              <h2 style="margin:0 0 8px;color:#0b3a63;font-size:17px;">Modalités de paiement</h2>
              <p style="margin:0 0 16px;color:#4a6a86;font-size:14px;">Vous avez deux possibilités pour régler votre formation :</p>

              <table role="presentation" width="100%" style="background:#f6faff;border:1px solid #e2ecf8;border-radius:12px;margin-bottom:12px;">
                <tr>
                  <td style="padding:16px 18px;color:#274b6b;font-size:14px;line-height:1.5;">
                    <strong style="color:#0b3a63;">1. Paiement avant la formation</strong><br>
                    Vous pouvez effectuer le paiement à l'avance afin de confirmer votre inscription.
                  </td>
                </tr>
              </table>

              <table role="presentation" width="100%" style="background:#f6faff;border:1px solid #e2ecf8;border-radius:12px;margin-bottom:26px;">
                <tr>
                  <td style="padding:16px 18px;color:#274b6b;font-size:14px;line-height:1.5;">
                    <strong style="color:#0b3a63;">2. Paiement sur place</strong><br>
                    Vous pouvez également régler sur place, avant le début de la séance.
                  </td>
                </tr>
              </table>

              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center" bgcolor="#25D366" style="border-radius:10px;">
                    <a href="{lien_whatsapp}"
                       target="_blank"
                       style="display:block;padding:15px 20px;font-size:15.5px;font-weight:bold;color:#ffffff;text-decoration:none;border-radius:10px;">
                      <img src="cid:{wa_cid[1:-1]}" alt="WhatsApp" width="22" height="22" style="vertical-align:middle;border:0;margin-right:10px;">Nous contacter
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
""",
        subtype="html",
    )

    # Badge incorporé au mail (image liée, affichée dans le corps) et aussi
    # téléchargeable comme pièce jointe.
    partie_html = msg.get_payload()[1]
    partie_html.add_related(badge_jpg, "image", "jpeg", cid=badge_cid, filename="badge.jpg")
    partie_html.add_related(wa_png, "image", "png", cid=wa_cid)

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

        # 2) Mail à l'inscrit (avec son badge) et notification à l'agence.
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
