"""Réception des évènements Stripe et envoi des alertes sur Telegram.

Stripe appelle POST /webhook/stripe dès qu'un évènement surveillé se produit — pas
de sondage, pas de délai : l'alerte part dans la seconde.

Codes de réponse, et ce que Stripe en fait :
  200  évènement acquitté, Stripe ne le renverra pas
  400  signature invalide : la requête ne vient pas de Stripe, on la jette
  503  on n'a PAS pu alerter mais ça peut encore marcher (Telegram momentanément
       injoignable, configuration incomplète). Stripe rejoue alors l'évènement
       pendant 3 jours, avec un espacement croissant. C'est le filet de sécurité :
       une alerte n'est jamais perdue parce que Telegram a hoqueté ou qu'une
       variable d'environnement a été mal collée.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import stripe
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import alertes, config, journal, rattrapage, recap, telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("alertes.serveur")


async def _travail_de_fond() -> None:
    """Rattrapage PUIS récapitulatifs, dans cet ordre, jamais en parallèle.

    L'ordre compte : recap.boucle() vérifie ses créneaux dès sa première seconde.
    Lancée en même temps que le rattrapage, elle pouvait poster le bilan de minuit
    sur une base encore vide — puis marquer le créneau comme fait, définitivement.
    Les bons chiffres n'auraient jamais été envoyés.

    Le rattrapage ne lève jamais (il absorbe ses erreurs), donc rien ne peut
    empêcher les récapitulatifs de démarrer ensuite.
    """
    await asyncio.to_thread(rattrapage.executer)
    if config.RECAP_ACTIF:
        await recap.boucle()


@asynccontextmanager
async def lifespan(app: FastAPI):
    journal.initialiser(config.DB_PATH)
    manquants = config.verifier_au_demarrage()
    if manquants:
        # Volontairement pas une exception : sur Railway, un process qui refuse de
        # démarrer boucle en redémarrages et le message d'erreur se noie. Un log
        # d'erreur au démarrage se retrouve en tête des logs du déploiement, et
        # /health répond 503 tant que ce n'est pas réglé.
        logger.error(
            "Configuration incomplète, les alertes NE PARTIRONT PAS : %s manquant(s).",
            ", ".join(manquants),
        )
    if not config.STRIPE_API_KEY:
        logger.warning(
            "STRIPE_API_KEY absente : les alertes afficheront les identifiants bruts "
            "(cus_xxx) au lieu des noms et emails."
        )
    logger.info("Évènements surveillés : %s", ", ".join(config.EVENEMENTS))

    if config.RECAP_ACTIF:
        logger.info(
            "Récapitulatifs Telegram à %s (heure de Paris).",
            ", ".join(f"{h}h" for h in config.RECAP_HEURES),
        )
    else:
        logger.info("Récapitulatifs désactivés (RECAP_ACTIF=false).")

    # Une seule tâche de fond, et la référence est gardée : asyncio ne retient
    # qu'une référence faible sur les tâches, une variable perdue peut donc être
    # ramassée par le garbage collector en plein travail, sans la moindre trace.
    tache = asyncio.create_task(_travail_de_fond())

    yield

    tache.cancel()


app = FastAPI(title="Alertes Stripe -> Telegram", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> JSONResponse:
    """Sonde de Railway. Répond TOUJOURS 200, même mal configurée.

    C'est délibéré, et c'était une erreur au départ : railway.json branche son
    healthcheck ici, avec redémarrage automatique en cas d'échec. Un 503 sur
    configuration incomplète faisait donc échouer le déploiement, redémarrer trois
    fois, puis déclarer le service mort — exactement ce qu'on voulait éviter. Pire :
    le conteneur ne servant plus rien, Stripe tombait sur une adresse morte au lieu
    du 503 qui, lui, déclenche le rejeu pendant 3 jours.

    Le service reste donc vivant et le dit dans son corps de réponse : `status` vaut
    "degraded" et `configuration_manquante` nomme la variable à corriger. C'est
    /webhook/stripe qui porte le vrai filet de sécurité, en répondant 503.
    """
    manquants = config.verifier_au_demarrage()
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok" if not manquants else "degraded",
            "configuration_manquante": manquants,
            "evenements_surveilles": config.EVENEMENTS,
            "enrichissement_stripe": bool(config.STRIPE_API_KEY),
            "recaps": (
                [f"{h}h" for h in config.RECAP_HEURES] if config.RECAP_ACTIF else "désactivés"
            ),
            "rattrapage": dict(rattrapage.ETAT),
        },
    )


def _jour_de_levenement(evenement: dict) -> str:
    """La date (à Paris) à laquelle la souscription a EU LIEU, pas celle où on la
    traite. Stripe rejoue une livraison ratée jusqu'à 3 jours : après une panne de
    Telegram ou un redéploiement, dater les souscriptions du moment du traitement les
    entasserait toutes sur aujourd'hui — gonflant le total du jour et laissant à
    zéro le bilan de la journée réellement concernée."""
    horodatage = evenement.get("created")
    if isinstance(horodatage, (int, float)) and horodatage > 0:
        return datetime.fromtimestamp(horodatage, tz=alertes.FUSEAU).date().isoformat()
    return datetime.now(alertes.FUSEAU).date().isoformat()


def _traiter(corps: bytes, signature: str | None) -> Response:
    """Tout le travail, en synchrone. Appelé dans un thread : les appels à Stripe
    (jusqu'à 80 s de timeout réseau), à Telegram et à sqlite bloquent, et les exécuter
    directement dans la boucle asyncio figerait le serveur entier — les évènements
    suivants s'empileraient jusqu'à dépasser le délai de livraison de Stripe, qui
    les rejouerait alors qu'ils sont encore en cours de traitement, produisant des
    doublons ; et /health cesserait de répondre pendant ce temps."""
    # Vérifier ce secret AVANT de s'en servir : sans lui, construct_event échoue en
    # « signature invalide », ce qui ferait chercher une tentative d'intrusion ou un
    # mauvais secret là où il n'y a qu'une variable oubliée. Et un 400 dit à Stripe
    # de ne pas rejouer, alors que la souscription, elle, mérite d'être rattrapée.
    if not config.STRIPE_WEBHOOK_SECRET:
        logger.error(
            "STRIPE_WEBHOOK_SECRET absent : impossible de vérifier la signature. "
            "Stripe rejouera l'évènement, renseigner la variable puis attendre."
        )
        return Response(status_code=503, content="secret de webhook absent")

    try:
        evenement = stripe.Webhook.construct_event(
            corps, signature, config.STRIPE_WEBHOOK_SECRET
        ).to_dict()
    except ValueError:
        logger.warning("Corps de requête illisible sur /webhook/stripe.")
        return Response(status_code=400, content="corps invalide")
    except stripe.SignatureVerificationError as exc:
        # Soit le secret configuré n'est pas le bon, soit la requête ne vient pas de
        # Stripe. Dans les deux cas on refuse — c'est ce qui empêche un tiers de
        # poster de fausses alertes dans le canal.
        logger.warning("Signature Stripe invalide : %s", exc)
        return Response(status_code=400, content="signature invalide")

    event_id = evenement.get("id") or ""
    type_evenement = evenement.get("type") or ""

    if type_evenement not in config.EVENEMENTS:
        logger.info("Évènement %s ignoré (non surveillé).", type_evenement)
        return Response(status_code=200, content="ignoré")

    if event_id and journal.deja_traite(config.DB_PATH, event_id):
        logger.info("Évènement %s déjà traité, pas de seconde alerte.", event_id)
        return Response(status_code=200, content="doublon")

    contexte = alertes.evaluer(evenement)
    if contexte is None:
        logger.info("Évènement %s sans alerte à envoyer.", type_evenement)
        return Response(status_code=200, content="pas d'alerte")

    # Une configuration incomplète est une panne RÉPARABLE : acquitter ici perdrait
    # définitivement une vraie souscription à cause d'une variable mal collée. On
    # répond 503 pour que Stripe rejoue pendant 3 jours — le temps de s'en rendre
    # compte et de corriger.
    manquants = config.verifier_au_demarrage()
    if manquants:
        logger.error(
            "Alerte %s NON envoyée : %s manquant(s) dans la configuration. "
            "Stripe rejouera l'évènement, corriger la variable puis attendre.",
            event_id, ", ".join(manquants),
        )
        return Response(status_code=503, content="configuration incomplète")

    # Compter la souscription AVANT de composer le message : l'alerte annonce le
    # total du jour, elle doit donc déjà s'y inclure. Les abonnements du mode test
    # de Stripe ne sont jamais comptés — ils fausseraient les totaux réels.
    # Le compte du jour figure sur TOUS les messages. Sur une alerte réelle il
    # s'inclut ("3e souscription aujourd'hui") ; sur une alerte de test il se
    # contente d'annoncer l'état réel de la journée, sans s'y compter.
    cumul = None
    if contexte["livemode"]:
        abonnement = contexte["abonnement"]
        montant, devise, periodicite, partiel = alertes.resume_chiffre(contexte)
        client = abonnement.get("customer")
        # Le MÊME jour des deux côtés : la souscription est datée de l'évènement
        # Stripe, le cumul annoncé doit donc porter sur ce jour-là. Lire
        # « aujourd'hui » ici ferait afficher « 1re souscription aujourd'hui » sur
        # un rejeu daté d'hier, alors que la journée en cours en compte peut-être
        # déjà cinq.
        jour = _jour_de_levenement(evenement)
        journal.enregistrer_souscription(
            config.DB_PATH, event_id, abonnement.get("id"),
            client.get("id") if isinstance(client, dict) else client,
            montant, devise, periodicite, jour, partiel,
        )
        cumul = alertes.phrase_cumul(journal.totaux(config.DB_PATH, jour, jour))
    else:
        aujourdhui = datetime.now(alertes.FUSEAU).date().isoformat()
        cumul = alertes.phrase_etat_journee(
            journal.totaux(config.DB_PATH, aujourdhui, aujourdhui)
        )

    texte = alertes.rendre(contexte, cumul)
    try:
        telegram.envoyer(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, texte)
    except telegram.TelegramErreurTemporaire as exc:
        logger.error("Telegram injoignable pour %s : %s — Stripe rejouera.", event_id, exc)
        return Response(status_code=503, content="telegram indisponible")
    except telegram.TelegramErreurPermanente as exc:
        # Telegram a bien répondu, mais refuse : jeton révoqué, bot retiré du groupe.
        # Rejouer 3 jours n'y changerait rien, donc on acquitte — mais on le crie
        # dans les logs, car l'alerte EST perdue.
        logger.error(
            "ALERTE PERDUE pour %s (%s) : Telegram refuse le message — %s. "
            "Vérifier TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID.",
            event_id, type_evenement, exc,
        )
        return Response(status_code=200, content="telegram refuse")

    if event_id:
        try:
            journal.marquer_traite(config.DB_PATH, event_id, type_evenement)
        except Exception as exc:
            # Le message est DÉJÀ parti sur Telegram. Laisser remonter l'exception
            # renverrait 500, Stripe rejouerait, et l'évènement n'étant pas
            # enregistré, l'alerte partirait une seconde fois.
            logger.error(
                "Alerte %s envoyée mais non journalisée (%s) : un doublon est "
                "possible si Stripe rejoue cet évènement.", event_id, exc,
            )

    logger.info("Alerte envoyée sur Telegram pour %s (%s).", event_id, type_evenement)
    return Response(status_code=200, content="ok")


@app.post("/webhook/stripe")
async def webhook_stripe(request: Request) -> Response:
    # Le corps BRUT, tel qu'envoyé : la signature est calculée octet par octet dessus.
    # Passer par un JSON déjà analysé puis ré-sérialisé (espaces, ordre des clés)
    # ferait échouer la vérification à tous les coups.
    corps = await request.body()
    signature = request.headers.get("stripe-signature")
    return await run_in_threadpool(_traiter, corps, signature)
