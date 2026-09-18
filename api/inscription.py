"""
api/inscription.py
Fonction serverless Vercel (runtime Python) qui traite le formulaire d'inscription
et envoie un email de notification via SMTP Gmail (smtplib, natif Python — aucune
dépendance externe nécessaire).

Route exposée automatiquement par Vercel : POST /api/inscription

Variables d'environnement à définir dans Vercel (Project Settings > Environment Variables) :
    GMAIL_ADRESSE       -> oneprocom.robotique@gmail.com
    GMAIL_MOT_DE_PASSE  -> mot de passe d'application Gmail (16 caractères, PAS le mot de passe du compte)
    DESTINATAIRE        -> adresse qui reçoit les notifications (par défaut = GMAIL_ADRESSE)
"""

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


WHATSAPP_NUMERO = "212716639486"  # format international, sans +, sans espaces
WHATSAPP_LIEN = f"https://wa.me/{WHATSAPP_NUMERO}"


def envoyer_mail_inscrit(nom, prenom, email, telephone, profession):
    """Mail envoyé à l'inscrit : sollicitation de paiement de la formation."""
    gmail_adresse = os.environ.get("GMAIL_ADRESSE", "oneprocom.robotique@gmail.com")
    gmail_mdp = os.environ.get("GMAIL_MOT_DE_PASSE")

    if not gmail_mdp:
        raise RuntimeError(
            "Variable d'environnement GMAIL_MOT_DE_PASSE manquante. "
            "Configurez-la dans Vercel (Project Settings > Environment Variables)."
        )

    msg = EmailMessage()
    msg["Subject"] = "Votre inscription — étape suivante : paiement de la formation"
    msg["From"] = gmail_adresse
    msg["To"] = email

    msg.set_content(
        f"Bonjour {prenom},\n\n"
        "Nous vous confirmons la bonne prise en compte de votre inscription.\n\n"
        "Pour valider définitivement votre place, merci de procéder au règlement "
        "de la formation :\n\n"
        "— Si vous suivez la formation en présentiel : le paiement peut être "
        "effectué avant la séance ou directement sur place, le jour de la formation.\n\n"
        "— Si vous suivez la formation en ligne : merci de nous rejoindre sur WhatsApp "
        f"au {WHATSAPP_NUMERO} ({WHATSAPP_LIEN}) afin d'échanger avec nous sur les "
        "modalités de paiement.\n\n"
        "Nous restons à votre disposition pour toute question.\n\n"
        "L'équipe One Pro Com"
    )
    msg.add_alternative(
        f"""
        <p>Bonjour {prenom},</p>
        <p>Nous vous confirmons la bonne prise en compte de votre inscription.</p>
        <p>Pour valider définitivement votre place, merci de procéder au règlement
        de la formation :</p>
        <ul>
            <li><strong>En présentiel</strong> : paiement possible avant la séance
            ou directement sur place, le jour de la formation.</li>
            <li><strong>En ligne</strong> : merci de nous rejoindre sur WhatsApp au
            <strong>{WHATSAPP_NUMERO}</strong> afin d'échanger sur les modalités de
            paiement : <a href="{WHATSAPP_LIEN}">{WHATSAPP_LIEN}</a></li>
        </ul>
        <p>Nous restons à votre disposition pour toute question.</p>
        <p>L'équipe One Pro Com</p>
        """,
        subtype="html",
    )

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(gmail_adresse, gmail_mdp)
        server.send_message(msg)


def envoyer_mail_admin(nom, prenom, email, telephone, profession):
    """Mail envoyé à l'agence : données structurées pour report dans le fichier Excel."""
    gmail_adresse = os.environ.get("GMAIL_ADRESSE", "oneprocom.robotique@gmail.com")
    gmail_mdp = os.environ.get("GMAIL_MOT_DE_PASSE")
    admin_email = os.environ.get("ADMIN_EMAIL", gmail_adresse)

    if not admin_email:
        return  # notification admin désactivée

    msg = EmailMessage()
    msg["Subject"] = f"Nouvelle inscription : {prenom} {nom}"
    msg["From"] = gmail_adresse
    msg["To"] = admin_email
    msg["Reply-To"] = email

    msg.set_content(
        "Nouvelle inscription à reporter dans le fichier Excel :\n\n"
        f"Nom : {nom}\n"
        f"Prénom : {prenom}\n"
        f"Email : {email}\n"
        f"Téléphone : {telephone}\n"
        f"Profession : {profession}\n"
    )
    msg.add_alternative(
        f"""
        <h2>Nouvelle inscription à reporter dans le fichier Excel</h2>
        <table cellpadding="6" cellspacing="0" border="1" style="border-collapse:collapse;">
            <tr><td><strong>Nom</strong></td><td>{nom}</td></tr>
            <tr><td><strong>Prénom</strong></td><td>{prenom}</td></tr>
            <tr><td><strong>Email</strong></td><td>{email}</td></tr>
            <tr><td><strong>Téléphone</strong></td><td>{telephone}</td></tr>
            <tr><td><strong>Profession</strong></td><td>{profession}</td></tr>
        </table>
        """,
        subtype="html",
    )

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(gmail_adresse, gmail_mdp)
        server.send_message(msg)


def envoyer_mail(nom, prenom, email, telephone, profession):
    """Envoie les deux mails : confirmation/paiement à l'inscrit + notification à l'agence."""
    envoyer_mail_inscrit(nom, prenom, email, telephone, profession)
    envoyer_mail_admin(nom, prenom, email, telephone, profession)


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
            self._repondre(code, html)
            return

        if not EMAIL_REGEX.match(email):
            code, html = page_html(
                "Erreur - Inscription",
                "<p>L'adresse email fournie n'est pas valide.</p>"
                "<p><a href=\"javascript:history.back()\">Retour au formulaire</a></p>",
                400,
            )
            self._repondre(code, html)
            return

        try:
            envoyer_mail(nom, prenom, email, telephone, profession)
            code, html = page_html(
                "Inscription confirmée",
                f"<h1>Merci {prenom} !</h1>"
                "<p>Votre inscription a bien été enregistrée. Un email de confirmation a été envoyé.</p>"
                "<p><a href='/'>Retour à l'accueil</a></p>",
                200,
            )
        except Exception as e:
            print("Erreur envoi mail inscription :", repr(e))
            code, html = page_html(
                "Erreur - Inscription",
                "<p>Votre inscription a été reçue mais l'email de confirmation n'a pas pu être envoyé. "
                "Nous vous contacterons directement.</p>"
                "<p><a href='/'>Retour à l'accueil</a></p>",
                502,
            )

        self._repondre(code, html)

    def _repondre(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))