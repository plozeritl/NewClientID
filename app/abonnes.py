"""MRR et nombre d'abonnés actifs, recalculés depuis les abonnements Stripe.

Pourquoi recalculer plutôt que demander à Stripe : l'Analytics API, qui sert les
chiffres du tableau de bord Billing, est en préversion fermée et renvoie 404 sur ce
compte. Il n'existe par ailleurs aucune métrique publique de nombre d'abonnés.

Les conventions suivent celles de Stripe, vérifiées sur le compte le 11/09/2026 :
  - le MRR compte les abonnements actifs ET en retard de paiement ;
  - les abonnés actifs ne comptent que les 'active' (3 701 affichés contre 4 061 en
    incluant les impayés) ;
  - un abonnement facturé tous les 28 à 31 jours vaut UN mois, sans prorata ;
  - les remises en cours sont déduites, les taxes ignorées ;
  - le facturé à l'usage n'a pas de MRR.

Deux écarts assumés avec le chiffre affiché par Stripe :
  - **Le taux de change est fixe** (`TAUX_EUR_USD`), là où Stripe utilise ses taux
    du jour. Le MRR affiché s'écartera un peu du sien.
  - **Les prix à paliers ne sont pas chiffrés** : leur client compte comme actif
    (il paie), mais sa contribution au MRR est inconnue et signalée dans ETAT.

Coût : la liste complète des abonnements, soit une requête par centaine — une
quarantaine, environ 45 s. Le résultat est gardé dix minutes.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app import config, stripe_client

logger = logging.getLogger("alertes.abonnes")

STATUTS_MRR = ("active", "past_due")
STATUTS_ABONNES = ("active",)

DUREE_CACHE = 600
_cache: dict = {"instant": 0.0, "valeur": None}

# Repris par /health : un MRR absent des messages doit pouvoir s'expliquer d'un
# coup d'œil, sans aller lire les journaux de Railway.
ETAT: dict = {"disponible": None, "probleme": None, "paliers_non_chiffres": 0,
              "calcule_le": None, "duree_s": None,
              # Le MRR recalculé n'est plus affiché dans les messages (trop
              # éloigné du tableau de bord), mais reste consultable ici.
              "mrr_eur_indicatif": None}

JOURS_PAR_MOIS = 365 / 12


# --- accès Stripe, isolés pour être remplaçables dans les tests -----------------

def _lister_abonnements(statut: str):
    return stripe_client.client_fond.v1.subscriptions.list(params={
        "status": statut, "limit": 100,
        # Le coupon d'une remise n'est qu'un identifiant tant qu'on ne le développe
        # pas. Sans ce développement, toutes les remises étaient ignorées et le MRR
        # gonflé d'autant — constaté le 11/09/2026.
        "expand": ["data.discounts.source.coupon"],
    }).auto_paging_iter()


_coupons: dict[str, dict] = {}


def _coupon(identifiant: str) -> dict | None:
    """Repli si le développement n'a pas eu lieu. Un compte a peu de coupons
    distincts : le cache évite de les redemander à chaque abonnement."""
    if identifiant not in _coupons:
        try:
            _coupons[identifiant] = stripe_client.client_fond.v1.coupons.retrieve(
                identifiant
            ).to_dict()
        except Exception as exc:
            logger.warning("Coupon %s illisible, remise ignorée : %s", identifiant, exc)
            _coupons[identifiant] = None
    return _coupons[identifiant]


# --- calcul --------------------------------------------------------------------

def _mois_par_periode(intervalle: str, nombre: int) -> float:
    """Combien de mois dure une période de facturation."""
    nombre = max(1, nombre or 1)
    if intervalle == "month":
        return float(nombre)
    if intervalle == "year":
        return 12.0 * nombre
    if intervalle == "week":
        return 7 * nombre / JOURS_PAR_MOIS
    if intervalle == "day":
        # 28 à 31 jours = un mois plein, comme Stripe. Au prorata, le MRR sortait
        # 8,6 % trop haut, le catalogue étant pour moitié facturé « tous les 28 jours ».
        return 1.0 if 28 <= nombre <= 31 else nombre / JOURS_PAR_MOIS
    return 0.0


def _mensualiser(montant: int, intervalle: str, nombre: int) -> float:
    mois = _mois_par_periode(intervalle, nombre)
    return montant / mois if mois else 0.0


def _coupon_de(remise: dict) -> dict | None:
    """Le coupon d'une remise, quelle que soit la forme renvoyée par Stripe :
    `coupon` (anciennes versions d'API) ou `source.coupon` (depuis 2026), lui-même
    objet développé ou simple identifiant."""
    brut = remise.get("coupon")
    if brut is None:
        brut = (remise.get("source") or {}).get("coupon")
    if isinstance(brut, dict):
        return brut
    if isinstance(brut, str) and brut:
        return _coupon(brut)
    return None


def _remise_en_cours(remise: dict) -> bool:
    fin = remise.get("end")
    if isinstance(fin, (int, float)) and fin > 0:
        return datetime.now(timezone.utc).timestamp() < fin
    return True


def _mrr_abonnement(abonnement: dict) -> tuple[str, float, bool]:
    """(devise, MRR en centimes, une part reste-t-elle non chiffrable ?)."""
    devise = ""
    total = 0.0
    mois_periode = 1.0
    non_chiffre = False

    for item in (abonnement.get("items") or {}).get("data") or []:
        price = item.get("price") or {}
        recurring = price.get("recurring") or {}
        if recurring.get("usage_type") == "metered":
            continue                       # pas de MRR connu d'avance, comme Stripe
        devise = devise or (price.get("currency") or "")
        mois_periode = _mois_par_periode(recurring.get("interval") or "",
                                         recurring.get("interval_count") or 1) or 1.0
        unitaire = price.get("unit_amount")
        if unitaire is None:
            non_chiffre = True             # prix à paliers : montant inconnu ici
            continue
        quantite = item.get("quantity")
        quantite = 1 if quantite is None else quantite
        total += _mensualiser(unitaire * quantite, recurring.get("interval") or "",
                              recurring.get("interval_count") or 1)

    # Les remises s'appliquent par FACTURE : un montant fixe se divise donc par la
    # durée de la période avant d'être retiré du mensuel. Sans ça, 20 € de remise
    # sur un annuel de 190 € effaçaient 20 € par mois au lieu de 1,67 €.
    remises = abonnement.get("discounts") or []
    if not remises and abonnement.get("discount"):
        remises = [abonnement["discount"]]
    for remise in remises:
        if not isinstance(remise, dict) or not _remise_en_cours(remise):
            continue
        coupon = _coupon_de(remise)
        if not coupon:
            continue
        if coupon.get("percent_off"):
            total *= 1 - (coupon["percent_off"] / 100)
        elif coupon.get("amount_off"):
            total = max(0.0, total - coupon["amount_off"] / mois_periode)

    return devise, total, non_chiffre


def _en_euros(mrr: dict[str, int]) -> int | None:
    """Le MRR total ramené en euros, ou None si une devise n'est pas convertible.
    Mieux vaut n'afficher aucun total qu'un total amputé d'une devise inconnue."""
    total = 0.0
    for devise, montant in mrr.items():
        if devise == "eur":
            total += montant
        elif devise == "usd":
            total += montant / config.TAUX_EUR_USD
        else:
            logger.warning("Devise %s non convertible : MRR global non affiché.", devise)
            return None
    return round(total)


def etat() -> dict | None:
    """{'mrr': {devise: centimes}, 'mrr_eur': centimes, 'abonnes': n},
    ou None si Stripe est inaccessible."""
    if not config.STRIPE_API_KEY:
        ETAT.update(disponible=False, probleme="STRIPE_API_KEY absente")
        return None

    if _cache["valeur"] is not None and time.time() - _cache["instant"] < DUREE_CACHE:
        return _cache["valeur"]

    debut = time.monotonic()
    mrr: dict[str, int] = {}
    clients_actifs: set[str] = set()
    paliers = 0
    try:
        for statut in STATUTS_MRR:
            for brut in _lister_abonnements(statut):
                abonnement = brut.to_dict()
                devise, mensuel, non_chiffre = _mrr_abonnement(abonnement)
                if non_chiffre:
                    paliers += 1
                if mensuel > 0 and devise:
                    mrr[devise] = mrr.get(devise, 0) + round(mensuel)
                # Un client compte comme actif s'il paie : MRR connu, ou prix à
                # paliers dont le montant nous échappe mais qui est bien facturé.
                if statut in STATUTS_ABONNES and (mensuel > 0 or non_chiffre):
                    client = abonnement.get("customer")
                    identifiant = client.get("id") if isinstance(client, dict) else client
                    if identifiant:
                        clients_actifs.add(identifiant)
    except Exception as exc:
        # PermissionError comprise : le message part sans la ligne du MRR, et
        # /health dit pourquoi.
        motif = ("permission « Subscriptions → Read » manquante"
                 if isinstance(exc, stripe_client.stripe.PermissionError)
                 else f"{type(exc).__name__}: {str(exc)[:120]}")
        logger.warning("MRR et abonnés actifs indisponibles : %s", motif)
        ETAT.update(disponible=False, probleme=motif)
        return None

    valeur = {"mrr": mrr, "mrr_eur": _en_euros(mrr), "abonnes": len(clients_actifs)}
    _cache.update(instant=time.time(), valeur=valeur)
    ETAT.update(disponible=True, probleme=None, paliers_non_chiffres=paliers,
                mrr_eur_indicatif=valeur["mrr_eur"],
                calcule_le=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                duree_s=round(time.monotonic() - debut, 1))
    if paliers:
        logger.info("%s abonnement(s) à paliers : comptés comme actifs, MRR non chiffré.", paliers)
    return valeur
