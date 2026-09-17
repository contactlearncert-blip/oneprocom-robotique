# Déploiement sur Vercel

## Structure

```
├── index.html          → page d'accueil (formulaire)
├── style.css
├── images/
│   ├── oneprocom.jpeg
│   └── eagledrone.jpeg
└── api/
    └── inscription.py  → fonction serverless Python (envoi de l'email)
```

Aucune dépendance externe : `smtplib` fait partie de la bibliothèque
standard Python, donc pas de `requirements.txt` nécessaire.

## Étapes de déploiement

1. Pousser ce dossier sur un dépôt GitHub (ou GitLab/Bitbucket).
2. Sur https://vercel.com → "Add New Project" → importer le dépôt.
3. Laisser les réglages par défaut (Vercel détecte automatiquement le
   runtime Python via le dossier `api/`).
4. **Avant de déployer** (ou juste après, puis redéployer), aller dans
   Project Settings → Environment Variables, et ajouter :

   | Nom                  | Valeur                                                    |
   |-----------------------|-----------------------------------------------------------|
   | `GMAIL_ADRESSE`       | `zangatecno@gmail.com`                                     |
   | `GMAIL_MOT_DE_PASSE`  | mot de passe d'application Gmail (16 caractères)           |
   | `DESTINATAIRE`        | (optionnel) adresse qui reçoit les notifications           |

5. Déployer.

## Générer le mot de passe d'application Gmail

1. Activer la validation en 2 étapes sur le compte `zangatecno@gmail.com` :
   https://myaccount.google.com/security
2. Aller sur https://myaccount.google.com/apppasswords
3. Générer un mot de passe d'application, le copier (16 caractères)
4. Le coller comme valeur de `GMAIL_MOT_DE_PASSE` dans Vercel — jamais
   dans le code source.

## Test en local (optionnel)

```bash
npm i -g vercel
vercel dev
```

Puis ouvrir http://localhost:3000
