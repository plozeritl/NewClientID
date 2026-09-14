"""Nombre d'abonnés actifs, compté depuis les abonnements Stripe.

Il n'existe aucune métrique publique de nombre d'abonnés : l'Analytics API, qui
sert les chiffres du tableau de bord Billing, est en préversion fermée et renvoie
404 sur ce compte. On compte donc nous-mêmes.

Convention de Stripe, vérifiée sur le compte les 11 et 14/09/2026 : un abonné
actif est un client dont au moins un abonnement 'active' lui coûte quelque chose
CE MOIS-CI. Les impayés ('past_due') n'en font pas partie (3 701 affichés contre
4 061 en les incluant). Un client avec deux abonnements compte pour un. Ne comptent
pas : un essai gratuit, et — c'est le piège — un abonnement dont un coupon couvre
tout le prix pendant la période en cours (« 20 € offerts » sur 19 €/mois) : Stripe
ne le compte qu'à partir du premier mois payé. Ces cas faisaient 70 clients de
trop le 14/09/2026 (3 755 comptés contre 3 685).

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
        # Les coupons sont des identifiants tant qu'on ne les développe pas ; sans
        # eux, impossible de savoir si une remise ramène le prix à zéro.
        "expand": ["data.discounts.source.coupon"],
    }).auto_paging_iter()


JOURS_PAR_MOIS = 365 / 12


def _mois_par_periode(recurring: dict) -> float:
    """Durée d'une période de facturation, en mois. 28 à 31 jours = un mois."""
    intervalle, nombre = recurring.get("interval"), max(1, recurring.get("interval_count") or 1)
    if intervalle == "month":
        return float(nombre)
    if intervalle == "year":
        return 12.0 * nombre
    if intervalle == "week":
        return 7 * nombre / JOURS_PAR_MOIS
    if intervalle == "day":
        return 1.0 if 28 <= nombre <= 31 else nombre / JOURS_PAR_MOIS
    return 1.0


def _coupon_de(remise: dict) -> dict | None:
    """Le coupon d'une remise, sous sa forme 2026 (`source.coupon`) ou ancienne
    (`coupon`). None s'il n'est pas développé."""
    brut = (remise.get("source") or {}).get("coupon") or remise.get("coupon")
    return brut if isinstance(brut, dict) else None


def _facture_quelque_chose(abonnement: dict, maintenant: float | None = None) -> bool:
    """Vrai si le client paie quelque chose pour la période en cours.

    Une ligne à paliers ou à l'usage compte toujours (montant inconnu ici, mais bien
    facturé). Un montant fixe compte s'il reste positif une fois les remises en
    cours déduites : un coupon « 20 € offerts » sur un abonnement à 19 € ramène la
    facture à zéro, et Stripe ne compte alors pas le client comme abonné actif.
    """
    maintenant = maintenant if maintenant is not None else datetime.now(timezone.utc).timestamp()
    mensuel = 0.0
    mois = 1.0
    for item in (abonnement.get("items") or {}).get("data") or []:
        price = item.get("price") or {}
        recurring = price.get("recurring") or {}
        if recurring.get("usage_type") == "metered":
            return True
        unitaire = price.get("unit_amount")
        if unitaire is None:
            return True
        mois = _mois_par_periode(recurring)
        quantite = item.get("quantity")
        mensuel += unitaire * (1 if quantite is None else quantite) / mois

    remises = abonnement.get("discounts") or []
    if not remises and abonnement.get("discount"):
        remises = [abonnement["discount"]]
    for remise in remises:
        if not isinstance(remise, dict):
            continue
        fin = remise.get("end")
        if isinstance(fin, (int, float)) and fin > 0 and fin <= maintenant:
            continue                                  # remise terminée
        coupon = _coupon_de(remise)
        if not coupon:
            continue
        if coupon.get("percent_off"):
            mensuel *= 1 - coupon["percent_off"] / 100
        elif coupon.get("amount_off"):
            # Une remise fixe s'applique par facture : ramenée au mois.
            mensuel -= coupon["amount_off"] / mois
    return mensuel > 0.005


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
