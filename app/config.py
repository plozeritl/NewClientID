"""Configuration, entièrement pilotée par variables d'environnement.

En local elles sont lues dans le fichier .env ; sur Railway, dans les variables
du service. Aucun secret n'est écrit dans le code.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Signature des webhooks Stripe (commence par whsec_). C'est la SEULE protection de
# l'endpoint, qui est forcément public : sans lui, n'importe qui pourrait poster de
# fausses alertes dans le groupe Telegram. L'app refuse de démarrer s'il est absent.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

# Clé API Stripe, en LECTURE SEULE (Customers + Products suffisent). Sert uniquement à
# remplacer « cus_123 » par « Marie Dupont — marie@exemple.fr » dans l'alerte. Facultative :
# sans elle les alertes affichent les identifiants bruts, rien ne casse.
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "").strip()

# Jeton du bot Telegram, donné par @BotFather. C'est un secret : qui l'a peut
# écrire partout où le bot est présent.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

# Identifiant du groupe qui reçoit les alertes. Négatif pour un groupe
# (ex. -1001234567890). `python outils/trouver_chat_id.py` le retrouve.
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Évènements Stripe qui déclenchent une alerte. Tout le reste est accepté puis ignoré
# (Stripe autorise à s'abonner à plus large que ce qu'on traite).
# 'updated' n'est PAS là pour signaler les modifications d'abonnement : il sert
# uniquement à rattraper une souscription dont le premier paiement a abouti avec
# retard (voir app/alertes.py). Aucune alerte n'en sort dans les autres cas.
EVENEMENTS = [
    e.strip()
    for e in os.environ.get(
        "STRIPE_EVENEMENTS",
        "customer.subscription.created,customer.subscription.updated",
    ).split(",")
    if e.strip()
]

# Journal des évènements déjà traités, pour ne pas alerter deux fois quand Stripe
# renvoie le même évènement (ce qui arrive : Stripe garantit « au moins une fois »,
# pas « exactement une fois »).
DB_PATH = Path(os.environ.get("DB_PATH") or str(BASE_DIR / "data" / "alertes.db"))

# Heures (Paris) des récapitulatifs postés sur Telegram. Par défaut, seul le bilan
# de minuit (0) : il clôt la journée écoulée et ajoute les sept derniers jours.
# D'autres heures (ex. "0,12,18") ajoutent des points sur la journée en cours ;
# retirés le 12/09/2026 à la demande de l'utilisateur, jugés trop fréquents.
RECAP_HEURES = sorted({
    int(h.strip())
    for h in os.environ.get("RECAP_HEURES", "0").split(",")
    if h.strip().isdigit() and 0 <= int(h.strip()) <= 23
})

# Mettre à "false" pour ne garder que les alertes de souscription, sans aucun
# récapitulatif horaire.
RECAP_ACTIF = os.environ.get("RECAP_ACTIF", "true").strip().lower() in ("1", "true", "yes")

# Secondes accordées à un appel Stripe d'enrichissement. Court volontairement :
# voir app/alertes.py pour la raison.
STRIPE_TIMEOUT = int(os.environ.get("STRIPE_TIMEOUT") or 8)

# Taux de change euro/dollar : combien de dollars vaut 1 euro. Sert uniquement à
# afficher un MRR unique en euros plutôt qu'une addition impossible « X € + Y $ ».
# C'est un taux FIXE, à réviser à la main : Stripe, lui, utilise ses taux du jour,
# donc le chiffre affiché s'écartera un peu du sien au fil du temps.
TAUX_EUR_USD = float(os.environ.get("TAUX_EUR_USD") or "1.16")

# Nombre de jours d'historique Stripe relus au démarrage pour remplir les
# compteurs (voir app/rattrapage.py). 7 jours couvrent le total de la journée ET
# celui des 7 derniers jours du bilan de minuit. 0 désactive le rattrapage.
RATTRAPAGE_JOURS = int(os.environ.get("RATTRAPAGE_JOURS") or 7)

PORT = int(os.environ.get("PORT") or 8000)


def verifier_au_demarrage() -> list[str]:
    """Les problèmes de configuration qui empêchent l'app de servir à quelque chose."""
    manquants = []
    if not STRIPE_WEBHOOK_SECRET:
        manquants.append("STRIPE_WEBHOOK_SECRET")
    if not TELEGRAM_BOT_TOKEN:
        manquants.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        manquants.append("TELEGRAM_CHAT_ID")
    return manquants
