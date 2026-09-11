"""Volume net encaissé, lu directement dans les mouvements du solde Stripe.

À ne pas confondre avec le « montant souscrit » que compte app/journal.py : celui-ci
additionne le prix catalogue des nouvelles souscriptions du jour, y compris celles
démarrées en essai gratuit — de l'argent pas encore encaissé. Le volume net, lui,
est l'argent réellement entré, renouvellements compris, moins les remboursements.

C'est la même définition que le « Net volume » du tableau de bord Stripe :
  ventes (charge, payment) − remboursements, litiges et annulations, + les
  remboursements qui ont échoué (l'argent revient).

Ce qui n'en fait PAS partie, et qui a failli fausser le calcul :
  - `payout`   : le virement vers le compte bancaire. L'argent change de poche,
                 il n'est pas perdu. Compté comme une sortie, il transformait
                 3 325 € encaissés en 487 € — un chiffre faux de 85 %.
  - `stripe_fee` : les frais de Stripe, qui se déduisent du net bancaire mais pas
                 du volume des ventes.

Les montants sont dans la devise de règlement du compte : Stripe a déjà converti
les paiements en dollars, ce qui donne un montant unique.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from app import config, stripe_client

logger = logging.getLogger("alertes.volume")

VENTES = {"charge", "payment"}
RETOURS = {"refund", "payment_refund", "payment_failure_refund", "refund_failure",
           "payment_reversal", "adjustment", "dispute", "dispute_reversal"}

# Une période donnée est refaite toutes les 60 s tant que Telegram ne répond pas :
# sans cache, une panne Telegram d'une demi-heure à minuit relançait 30 fois la
# lecture d'une semaine de mouvements. Cinq minutes suffisent, les chiffres d'une
# journée close ne bougent plus et ceux du jour ne sont relus qu'au récapitulatif
# suivant.
DUREE_CACHE = 300
_cache: dict[tuple[str, str], tuple[float, dict]] = {}

ETAT: dict = {"disponible": None, "probleme": None, "calcule_le": None}


def _lister_mouvements(debut: datetime, fin: datetime):
    return stripe_client.client_fond.v1.balance_transactions.list(params={
        "created": {"gte": int(debut.timestamp()), "lt": int(fin.timestamp())},
        "limit": 100,
    }).auto_paging_iter()


def net_de_la_journee(jour: datetime) -> dict[str, int] | None:
    """{devise: centimes} pour la journée de `jour` (heure de Paris)."""
    return net_sur_periode(jour, jour)


def net_sur_periode(premier_jour: datetime, dernier_jour: datetime) -> dict[str, int] | None:
    """{devise: centimes} du début de `premier_jour` à la fin de `dernier_jour`.

    None si Stripe est inaccessible ou si la clé n'a pas la permission : le
    récapitulatif se contente alors d'omettre le chiffre, plutôt que de ne pas
    partir ou d'annoncer zéro — « je ne sais pas » et « rien » ne se confondent pas.
    """
    if not config.STRIPE_API_KEY:
        ETAT.update(disponible=False, probleme="STRIPE_API_KEY absente")
        return None

    debut = premier_jour.replace(hour=0, minute=0, second=0, microsecond=0)
    fin = dernier_jour.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    cle = (debut.isoformat(), fin.isoformat())
    en_cache = _cache.get(cle)
    if en_cache and time.time() - en_cache[0] < DUREE_CACHE:
        return en_cache[1]

    totaux: dict[str, int] = {}
    try:
        for brut in _lister_mouvements(debut, fin):
            mouvement = brut.to_dict()
            nature = mouvement.get("type")
            if nature not in VENTES and nature not in RETOURS:
                continue
            devise = mouvement.get("currency") or ""
            totaux[devise] = totaux.get(devise, 0) + (mouvement.get("amount") or 0)
    except Exception as exc:
        motif = ("permission « Balance transactions → Read » manquante"
                 if isinstance(exc, stripe_client.stripe.PermissionError)
                 else f"{type(exc).__name__}: {str(exc)[:120]}")
        logger.warning("Volume net indisponible : %s", motif)
        ETAT.update(disponible=False, probleme=motif)
        return None

    _cache[cle] = (time.time(), totaux)
    ETAT.update(disponible=True, probleme=None,
                calcule_le=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return totaux
