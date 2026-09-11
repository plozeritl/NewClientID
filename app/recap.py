"""Récapitulatifs horaires postés sur Telegram.

Cinq points dans la journée (12h, 16h, 18h, 20h, 22h) donnent l'état de la journée
en cours depuis minuit. Celui de minuit clôt la journée écoulée et y ajoute les sept
derniers jours.

Tout est calé sur l'heure de Paris, y compris le découpage des journées.

Pourquoi une boucle qui se réveille chaque minute plutôt qu'un vrai planificateur :
le service peut redémarrer à tout moment (redéploiement Railway, plantage). Une
minuterie posée au démarrage serait perdue à chaque redémarrage. Ici, l'état vit en
base — `recaps_envoyes` dit ce qui est déjà parti — donc un créneau manqué pendant
une coupure est rattrapé dès le retour du service, et jamais envoyé deux fois.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app import alertes, config, journal, telegram

logger = logging.getLogger("alertes.recap")

INTERVALLE_VERIFICATION = 60      # secondes entre deux vérifications

# Nombre de journées passées dont on rattrape encore le bilan de minuit. À ne pas
# confondre avec config.RATTRAPAGE_JOURS, qui dit sur combien de jours l'historique
# Stripe est relu au démarrage — les deux se répondent (voir app/rattrapage.py). Sans ça,
# une coupure de plus de 24 h perdait définitivement le bilan des jours traversés :
# seuls les créneaux de la date du jour étaient examinés.
RATTRAPAGE_BILANS_JOURS = 3


def _maintenant() -> datetime:
    return datetime.now(alertes.FUSEAU)


def _cle(creneau: datetime) -> str:
    return f"{creneau.date().isoformat()} {creneau.hour}"


def _creneaux_a_considerer(maintenant: datetime) -> list[datetime]:
    """Les créneaux du jour, plus les bilans de minuit des journées précédentes.

    Seul minuit est rattrapé sur les jours passés : un « point du jour » d'avant-hier
    n'intéresse plus personne, alors qu'un bilan quotidien manquant laisse un trou
    dans l'historique.
    """
    creneaux = []
    if 0 in config.RECAP_HEURES:
        for recul in range(RATTRAPAGE_BILANS_JOURS, 0, -1):
            veille = maintenant - timedelta(days=recul)
            creneaux.append(veille.replace(hour=0, minute=0, second=0, microsecond=0))
    for heure in config.RECAP_HEURES:
        creneaux.append(maintenant.replace(hour=heure, minute=0, second=0, microsecond=0))
    return sorted(creneaux)


def _totaux_du_jour(jour: datetime) -> dict:
    date = jour.date().isoformat()
    return journal.totaux(config.DB_PATH, date, date)


def _totaux_semaine(dernier_jour: datetime) -> dict:
    """Les sept derniers jours glissants, celui de `dernier_jour` compris."""
    debut = (dernier_jour - timedelta(days=6)).date().isoformat()
    return journal.totaux(config.DB_PATH, debut, dernier_jour.date().isoformat())


def construire(creneau: datetime) -> str:
    """Le message d'un créneau. Minuit clôt la veille ; les autres font le point sur
    la journée en cours."""
    if creneau.hour == 0:
        veille = creneau - timedelta(days=1)
        return alertes.message_bilan(veille, _totaux_du_jour(veille), _totaux_semaine(veille))
    return alertes.message_point_du_jour(creneau.hour, _totaux_du_jour(creneau))


def _envoyer(creneau: datetime) -> bool:
    """Retourne True si le créneau doit être marqué comme fait — envoi réussi, ou
    échec définitif qu'il est inutile de rejouer."""
    texte = construire(creneau)
    try:
        telegram.envoyer(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, texte)
    except telegram.TelegramErreurTemporaire as exc:
        # Non marqué : la vérification suivante (dans une minute) réessaiera.
        logger.error("Récapitulatif %s non envoyé : %s", _cle(creneau), exc)
        return False
    except telegram.TelegramErreurPermanente as exc:
        # Jeton révoqué, bot retiré du groupe : réessayer toutes les minutes jusqu'à
        # la fin des temps ne servirait qu'à remplir les journaux de 1440 erreurs par
        # jour. On note le créneau comme fait, et on le dit une bonne fois.
        logger.error(
            "Récapitulatif %s ABANDONNÉ : Telegram refuse le message — %s. "
            "Vérifier TELEGRAM_BOT_TOKEN ; les prochains échoueront pareil.",
            _cle(creneau), exc,
        )
        return True
    logger.info("Récapitulatif %s envoyé.", _cle(creneau))
    return True


def verifier_une_fois(maintenant: datetime | None = None) -> list[str]:
    """Envoie ce qui est dû. Retourne les créneaux effectivement envoyés.

    Quand plusieurs « points du jour » sont en retard (service arrêté une bonne
    partie de la journée), un seul est envoyé — le plus récent, le seul encore
    d'actualité — et les autres sont marqués sans être postés : personne n'a envie
    de recevoir d'un coup les récapitulatifs de 12h, 16h et 18h à 19h. Les bilans de
    minuit, eux, partent tous : chacun est la trace d'une journée différente.
    """
    maintenant = maintenant or _maintenant()
    dus = [c for c in _creneaux_a_considerer(maintenant)
           if maintenant >= c and not journal.recap_deja_envoye(config.DB_PATH, _cle(c))]
    if not dus:
        return []

    minuits = [c for c in dus if c.hour == 0]
    autres = [c for c in dus if c.hour != 0]
    a_envoyer = sorted(minuits + autres[-1:] if autres else minuits)

    for creneau in autres[:-1]:
        journal.marquer_recap_envoye(config.DB_PATH, _cle(creneau))
        logger.info("Récapitulatif %s périmé, passé sans envoi.", _cle(creneau))

    envoyes = []
    for creneau in a_envoyer:
        if _envoyer(creneau):
            journal.marquer_recap_envoye(config.DB_PATH, _cle(creneau))
            envoyes.append(_cle(creneau))
    return envoyes


def neutraliser_creneaux_passes() -> None:
    """Au tout premier démarrage seulement : marque comme faits les créneaux déjà
    passés. Sans ça, un premier déploiement à 21h enverrait d'un coup les
    récapitulatifs de minuit, 12h, 16h, 18h et 20h — sur une base vide, donc tous
    à zéro."""
    if not journal.aucun_recap_enregistre(config.DB_PATH):
        return
    maintenant = _maintenant()
    passes = [c for c in _creneaux_a_considerer(maintenant) if maintenant >= c]
    for creneau in passes:
        journal.marquer_recap_envoye(config.DB_PATH, _cle(creneau))
    if passes:
        logger.info(
            "Premier démarrage : %s créneaux déjà passés neutralisés.", len(passes)
        )


async def boucle() -> None:
    # Dans le try, et dans un thread, comme le reste : c'est un appel sqlite, et il
    # se produit au démarrage — moment où le volume Railway peut encore être
    # inaccessible. Laissé dehors, la moindre erreur tuait la tâche pour de bon :
    # plus aucun récapitulatif, et rien pour le signaler (/health répondait "ok").
    while True:
        try:
            await asyncio.to_thread(neutraliser_creneaux_passes)
            await asyncio.to_thread(verifier_une_fois)
        except Exception:
            # Une boucle de fond ne doit jamais mourir : sans ça, un incident
            # ponctuel arrêterait définitivement tous les récapitulatifs, sans que
            # rien ne le signale (les alertes, elles, continueraient de marcher).
            logger.exception("Erreur dans la boucle des récapitulatifs, on continue.")
        await asyncio.sleep(INTERVALLE_VERIFICATION)
