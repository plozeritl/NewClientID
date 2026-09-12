"""Envoie une fausse alerte dans le groupe Telegram configuré.

Sert à vérifier que le jeton et l'identifiant de groupe sont bons, et que le
message s'affiche correctement, sans attendre qu'un vrai client s'abonne.

Lancer :  python outils/tester_telegram.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import alertes, config, journal, telegram, volume  # noqa: E402
from datetime import datetime  # noqa: E402

EXEMPLE = {
    "id": "evt_exemple",
    "type": "customer.subscription.created",
    "livemode": False,          # titre « 🧪 Abonnement de test », jamais compté
    "data": {"object": {
        "id": "sub_exemple",
        "status": "active",
        "customer": {"id": "cus_exemple", "name": "Client de démonstration",
                     "email": "demo@exemple.fr"},
        "start_date": 1789084800,
        "items": {"data": [{"quantity": 1, "price": {
            "id": "price_exemple", "nickname": "Formule de démonstration",
            "unit_amount": 2900, "currency": "eur",
            "recurring": {"interval": "month", "interval_count": 1},
        }}]},
    }},
}


def main() -> int:
    manquants = config.verifier_au_demarrage()
    manquants = [m for m in manquants if m.startswith("TELEGRAM")]
    if manquants:
        print(f"Manque dans le .env : {', '.join(manquants)}")
        return 1

    # Comme le vrai serveur : le compte réel de la journée figure sur le message,
    # sans que l'exemple s'y compte. Sinon cet outil ne montrerait pas ce qui part
    # vraiment en production.
    journal.initialiser(config.DB_PATH)
    jour = datetime.now(alertes.FUSEAU).date().isoformat()
    maintenant = datetime.now(alertes.FUSEAU)
    etat = alertes.phrase_etat_journee(journal.totaux(config.DB_PATH, jour, jour),
                                       volume.net_de_la_journee(maintenant))
    texte = alertes.formater(EXEMPLE, etat=etat)
    print("Message qui va être envoyé :\n")
    print(texte)
    print()

    try:
        telegram.envoyer(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, texte)
    except telegram.TelegramErreurPermanente as exc:
        print(f"Telegram a refusé le message : {exc}")
        print("Jeton invalide, mauvais chat_id, ou bot retiré du groupe.")
        return 1
    except telegram.TelegramErreurTemporaire as exc:
        print(f"Telegram est injoignable pour l'instant : {exc}")
        return 1

    print("Envoyé. Le message doit apparaître dans le groupe Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
