"""Retrouve l'identifiant du groupe Telegram où envoyer les alertes.

Telegram ne fournit pas d'annuaire : l'identifiant d'un groupe n'apparaît que dans
les messages que le bot a vus passer. Il faut donc, DANS L'ORDRE :
  1. ajouter le bot au groupe,
  2. écrire n'importe quel message dans ce groupe,
  3. lancer ce script.

Lancer :  python outils/trouver_chat_id.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, telegram  # noqa: E402


def main() -> int:
    if not config.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN n'est pas rempli dans le fichier .env.")
        return 1

    try:
        chats = telegram.chats_connus(config.TELEGRAM_BOT_TOKEN)
    except Exception as exc:
        print(f"Impossible d'interroger Telegram : {exc}")
        return 1

    if not chats:
        print("Aucune conversation trouvée. Dans l'ordre :")
        print("  1. ajouter le bot au groupe ;")
        print("  2. écrire un message DANS le groupe (après l'ajout) ;")
        print("  3. relancer ce script.")
        return 1

    print("Conversations connues du bot :\n")
    for chat in chats:
        nature = "groupe" if str(chat["id"]).startswith("-") else "conversation privée"
        print(f"  {chat['id']:<18} {nature:<20} {chat['titre'] or ''}")
    print("\nCopier l'identifiant du groupe voulu dans TELEGRAM_CHAT_ID du .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
