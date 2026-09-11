"""Remplissage des compteurs à partir de l'historique Stripe, au démarrage.

Sans ça, les totaux ne comptent que ce qui est arrivé DEPUIS la mise en service :
le premier jour, une alerte annoncerait « 1re souscription aujourd'hui » alors que
la journée en compte peut-être déjà cinq, et le bilan de minuit afficherait une
semaine vide.

Le rattrapage relit les évènements des derniers jours via l'API Stripe et enregistre
les souscriptions manquantes. Il n'envoie AUCUNE alerte : ces souscriptions sont du
passé, les annoncer maintenant serait du bruit. Il ne fait que rendre les chiffres
justes.

Sans effet de bord si tout est déjà là : l'enregistrement se fait par `event_id`
avec INSERT OR IGNORE, donc relancer le rattrapage à chaque démarrage ne compte
jamais deux fois la même souscription.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import stripe

from app import alertes, config, journal, recap

logger = logging.getLogger("alertes.rattrapage")

# Repris par /health : sans ça, un rattrapage refusé faute de permission ne se
# voyait que dans une ligne de log, et les compteurs restaient silencieusement
# à zéro.
ETAT: dict = {"execute": False, "souscriptions": 0, "probleme": None}


def _jour_de(evenement: dict) -> str:
    horodatage = evenement.get("created")
    if isinstance(horodatage, (int, float)) and horodatage > 0:
        return datetime.fromtimestamp(horodatage, tz=alertes.FUSEAU).date().isoformat()
    return datetime.now(alertes.FUSEAU).date().isoformat()


def executer() -> int:
    """Enregistre les souscriptions manquantes. Retourne le nombre ajouté."""
    if not config.RATTRAPAGE_JOURS:
        ETAT.update(execute=True, probleme="désactivé (RATTRAPAGE_JOURS=0)")
        return 0
    if not stripe.api_key:
        logger.info("Rattrapage impossible sans STRIPE_API_KEY : compteurs laissés vides.")
        ETAT.update(execute=True, probleme="STRIPE_API_KEY absente")
        return 0

    # Ancré à MINUIT, et non à l'heure courante : les totaux raisonnent en journées
    # entières, une fenêtre glissante laisserait le jour le plus ancien à moitié
    # rempli. Et on remonte plus loin que RATTRAPAGE_JOURS : un bilan de minuit en
    # retard (jusqu'à recap.RATTRAPAGE_BILANS_JOURS jours) affiche lui-même les 7
    # jours qui le précèdent — sans cette marge, il sortirait avec une semaine
    # partiellement vide.
    jours = max(0, config.RATTRAPAGE_JOURS) + recap.RATTRAPAGE_BILANS_JOURS
    depuis = (datetime.now(alertes.FUSEAU) - timedelta(days=jours)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    ajoutes = 0
    vus = 0
    try:
        evenements = stripe.Event.list(
            types=config.EVENEMENTS,
            created={"gte": int(depuis.timestamp())},
            limit=100,
        ).auto_paging_iter()

        for brut in evenements:
            evenement = brut.to_dict()
            vus += 1
            contexte = alertes.evaluer(evenement)
            if contexte is None or not contexte["livemode"]:
                continue

            event_id = evenement.get("id") or ""
            if not event_id:
                continue

            abonnement = contexte["abonnement"]
            # Version sans réseau : le rattrapage n'affiche jamais les noms de
            # formules, inutile d'interroger Stripe une fois par ligne.
            montant, devise, periodicite, partiel = alertes.resume_chiffre_sans_reseau(
                abonnement
            )
            client = abonnement.get("customer")
            if journal.enregistrer_souscription(
                config.DB_PATH, event_id, abonnement.get("id"),
                client.get("id") if isinstance(client, dict) else client,
                montant, devise, periodicite, _jour_de(evenement), partiel,
            ):
                ajoutes += 1
    except stripe.PermissionError:
        # Retourne ce qui a déjà été écrit : auto_paging_iter fait plusieurs appels,
        # un refus au milieu de la pagination ne doit pas effacer le décompte.
        logger.warning(
            "Rattrapage incomplet : la clé Stripe ne peut pas lire les évènements. "
            "Ajouter la permission « Events (Read) » sur la clé restreinte, sinon "
            "les compteurs partent de zéro."
        )
        ETAT.update(execute=True, souscriptions=ajoutes,
                    probleme="permission « Events (Read) » manquante sur la clé Stripe")
        return ajoutes
    except Exception:
        # Jamais bloquant : une pipeline qui alerte avec des compteurs incomplets
        # vaut infiniment mieux qu'une pipeline qui refuse de démarrer.
        logger.exception("Rattrapage interrompu ; les alertes fonctionnent quand même.")
        ETAT.update(execute=True, souscriptions=ajoutes, probleme="interrompu par une erreur")
        return ajoutes

    logger.info(
        "Rattrapage : %s évènement(s) relus depuis le %s, %s souscription(s) "
        "nouvellement enregistrée(s) (celles déjà connues sont ignorées).",
        vus, depuis.date().isoformat(), ajoutes,
    )
    ETAT.update(execute=True, souscriptions=ajoutes, probleme=None)
    return ajoutes
