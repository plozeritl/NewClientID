"""Envoi d'un message dans un groupe Telegram.

Deux informations suffisent :
  - le jeton du bot, donné par @BotFather ;
  - l'identifiant du groupe (`chat_id`), négatif pour un groupe.

Les messages partent en HTML (`parse_mode=HTML`) : Telegram n'y interprète que
<b>, <i>, <a>, <code> et quelques autres, et exige que tout le reste ait ses
&, < et > échappés — ce dont s'occupe app/alertes.py pour chaque valeur venue
de Stripe.
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger("alertes.telegram")

API = "https://api.telegram.org"
TIMEOUT = 6
TENTATIVES = 2
PAUSE_ENTRE_TENTATIVES = 1.5


class TelegramErreurPermanente(Exception):
    """Rejouer n'y changerait rien : jeton invalide, bot retiré du groupe, mauvais
    chat_id, HTML malformé. Il faut corriger la configuration."""


class TelegramErreurTemporaire(Exception):
    """Panne passagère (réseau, 5xx, limitation de débit) : vaut la peine d'être rejoué."""


def envoyer(jeton: str, chat_id: str, texte: str) -> None:
    """Poste un message. Lève TelegramErreurPermanente ou TelegramErreurTemporaire."""
    if not jeton or not chat_id:
        raise TelegramErreurPermanente(
            "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID n'est pas configuré."
        )

    url = f"{API}/bot{jeton}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texte,
        "parse_mode": "HTML",
        # Sans ça, le lien vers Stripe déclenche un encart d'aperçu qui double la
        # hauteur du message dans le fil.
        "disable_web_page_preview": True,
    }

    derniere_erreur: Exception | None = None
    for tentative in range(1, TENTATIVES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            derniere_erreur = TelegramErreurTemporaire(f"Telegram injoignable : {exc}")
        else:
            if resp.status_code < 300:
                return
            # Telegram explique toujours son refus dans 'description' ; le code HTTP
            # seul ne dit pas s'il s'agit d'un jeton mort ou d'un groupe quitté.
            try:
                motif = (resp.json() or {}).get("description") or resp.text
            except ValueError:
                motif = resp.text
            motif = (motif or "").strip()[:200]

            # 429 est une limitation de débit, donc temporaire, malgré son code 4xx.
            if resp.status_code == 429 or resp.status_code >= 500:
                derniere_erreur = TelegramErreurTemporaire(f"Telegram {resp.status_code} : {motif}")
            else:
                raise TelegramErreurPermanente(f"Telegram {resp.status_code} : {motif}")

        if tentative < TENTATIVES:
            time.sleep(PAUSE_ENTRE_TENTATIVES)

    raise derniere_erreur if derniere_erreur else TelegramErreurTemporaire("Échec Telegram inconnu.")


def chats_connus(jeton: str) -> list[dict]:
    """Les conversations où le bot a vu passer quelque chose récemment.

    Sert à trouver le chat_id d'un groupe sans le chercher à la main : Telegram ne
    donne pas d'annuaire, l'identifiant n'apparaît que dans les mises à jour reçues.
    Un message doit donc avoir été envoyé dans le groupe APRÈS l'ajout du bot.
    """
    resp = requests.get(f"{API}/bot{jeton}/getUpdates", timeout=15)
    if resp.status_code != 200:
        raise TelegramErreurPermanente(f"getUpdates a répondu {resp.status_code} : {resp.text[:200]}")

    vus: dict[str, dict] = {}
    for maj in (resp.json() or {}).get("result") or []:
        for cle in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (maj.get(cle) or {}).get("chat")
            if chat and str(chat.get("id")) not in vus:
                vus[str(chat.get("id"))] = {
                    "id": str(chat.get("id")),
                    "type": chat.get("type"),
                    "titre": chat.get("title") or chat.get("username") or chat.get("first_name"),
                }
    return list(vus.values())
