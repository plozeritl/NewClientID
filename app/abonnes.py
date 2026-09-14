"""Nombre d'abonnés actifs, compté depuis les abonnements Stripe.

Il n'existe aucune métrique publique de nombre d'abonnés : l'Analytics API, qui
sert les chiffres du tableau de bord Billing, est en préversion fermée et renvoie
404 sur ce compte. On compte donc nous-mêmes.

Convention de Stripe, vérifiée sur le compte les 11 et 14/09/2026 : un abonné
actif est un client avec au moins un abonnement au statut 'active' dont le prix
n'est pas nul. Les impayés ('past_due') n'en font pas partie (3 701 affichés
contre 4 061 en les incluant). Un client avec deux abonnements compte pour un ; un
client en essai gratuit, qui ne paie encore rien, ne compte pas.

Les COUPONS NE SONT PAS DÉDUITS, et c'est voulu. Le 14/09/2026, 70 clients avaient
un coupon couvrant 100 % du prix (presque tous « pour toujours ») ; les exclure
donnait 3 685 quand le tableau de bord affichait 3 736, alors que les compter
donnait 3 755 — l'écart de 19 étant le retard du tableau de bord, rafraîchi une
fois par jour (30 activations sur la journée). Stripe compte donc un client à
−100 % comme actif. Ne pas « corriger » ça une seconde fois.

Le tableau de bord ayant jusqu'à un jour de retard, un écart de quelques dizaines
à la hausse entre le bilan et Stripe est normal, pas une erreur.

Le MRR n'est plus calculé ici : recalculé à partir des abonnements, il s'écartait
de quelques pourcents du tableau de bord (taux de change, règles internes de
Stripe) et a été retiré le 12/09/2026. Le jour où l'Analytics API sera ouverte sur
ce compte, `revenue.mrr` donnera le chiffre exact.

Coût : la liste des abonnements actifs, une requête par centaine — une trentaine,
environ 30 s, une fois par bilan de minuit. Le résultat est gardé dix minutes pour
qu'une reprise après échec d'envoi ne recommence pas.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app import config, stripe_client

logger = logging.getLogger("alertes.abonnes")

DUREE_CACHE = 600
_cache: dict = {"instant": 0.0, "valeur": None}

# Repris par /health : une deuxième ligne absente du bilan doit pouvoir
# s'expliquer d'un coup d'œil, sans aller lire les journaux de Railway.
ETAT: dict = {"disponible": None, "probleme": None, "calcule_le": None, "duree_s": None}


def _lister_abonnements_actifs():
    """Isolé pour être remplaçable dans les tests. Client de fond : plusieurs
    pages, avec nouvelles tentatives."""
    return stripe_client.client_fond.v1.subscriptions.list(params={
        "status": "active", "limit": 100,
    }).auto_paging_iter()


def _facture_quelque_chose(abonnement: dict) -> bool:
    """Vrai si au moins une ligne a un prix : montant fixe non nul, prix à paliers
    (montant inconnu ici mais bien facturé) ou facturation à l'usage."""
    for item in (abonnement.get("items") or {}).get("data") or []:
        price = item.get("price") or {}
        recurring = price.get("recurring") or {}
        if recurring.get("usage_type") == "metered":
            return True
        unitaire = price.get("unit_amount")
        if unitaire is None or unitaire > 0:
            return True
    return False


def etat() -> dict | None:
    """{'abonnes': n}, ou None si Stripe est inaccessible."""
    if not config.STRIPE_API_KEY:
        ETAT.update(disponible=False, probleme="STRIPE_API_KEY absente",
                    calcule_le=None, duree_s=None)
        return None

    if _cache["valeur"] is not None and time.time() - _cache["instant"] < DUREE_CACHE:
        return _cache["valeur"]

    debut = time.monotonic()
    clients: set[str] = set()
    try:
        for brut in _lister_abonnements_actifs():
            abonnement = brut.to_dict()
            if not _facture_quelque_chose(abonnement):
                continue
            client = abonnement.get("customer")
            identifiant = client.get("id") if isinstance(client, dict) else client
            if identifiant:
                clients.add(identifiant)
    except Exception as exc:
        motif = ("permission « Subscriptions → Read » manquante"
                 if isinstance(exc, stripe_client.stripe.PermissionError)
                 else f"{type(exc).__name__}: {str(exc)[:120]}")
        logger.warning("Abonnés actifs indisponibles : %s", motif)
        # Tout est remis à zéro : un état « indisponible » qui garderait la date
        # du dernier succès laisserait croire à un chiffre à jour.
        ETAT.update(disponible=False, probleme=motif, calcule_le=None, duree_s=None)
        return None

    valeur = {"abonnes": len(clients)}
    _cache.update(instant=time.time(), valeur=valeur)
    ETAT.update(disponible=True, probleme=None,
                calcule_le=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                duree_s=round(time.monotonic() - debut, 1))
    return valeur
