"""Tests de la pipeline. Lancer : python tests/test_alertes.py

Volontairement sans pytest : un simple script, exécutable tel quel, qui affiche
ce qu'il vérifie et sort en erreur au premier échec.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

# Configuration de test, posée AVANT d'importer l'app (config.py lit l'environnement
# au moment de l'import).
SECRET_TEST = "whsec_secretdetest"
os.environ["STRIPE_WEBHOOK_SECRET"] = SECRET_TEST
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:JETON-DE-TEST"
os.environ["TELEGRAM_CHAT_ID"] = "-1001234567890"
os.environ["STRIPE_API_KEY"] = ""          # pas d'enrichissement : aucun appel réseau
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")

from app import abonnes, alertes, config, journal, rattrapage, recap, telegram, volume  # noqa: E402
from datetime import datetime  # noqa: E402
from app.serveur import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

reussis = 0


def verifier(condition: bool, libelle: str) -> None:
    global reussis
    if not condition:
        print(f"  ÉCHEC : {libelle}")
        sys.exit(1)
    reussis += 1
    print(f"  ok — {libelle}")


# --- mise en forme des montants ------------------------------------------------

def test_montants() -> None:
    print("\nMontants")
    verifier(alertes._montant(2900, "eur") == "29,00 €", "2900 centimes -> 29,00 €")
    verifier(alertes._montant(129900, "eur") == "1 299,00 €", "séparateur de milliers")
    verifier(alertes._montant(1500, "usd") == "15,00 $", "dollars")
    verifier(alertes._montant(5000, "jpy") == "5 000 ¥" or alertes._montant(5000, "jpy") == "5 000 JPY",
             "yen : pas de division par 100 (devise sans centimes)")
    verifier(alertes._montant(None, "eur") is None, "montant absent -> None")
    verifier(alertes._montant(2900, None) is None, "devise absente -> None")


def test_periodicite() -> None:
    print("\nPériodicité")
    verifier(alertes._periodicite({"recurring": {"interval": "month", "interval_count": 1}}) == "/ mois",
             "mensuel")
    verifier(alertes._periodicite({"recurring": {"interval": "month", "interval_count": 3}}) == "/ 3 mois",
             "trimestriel")
    verifier(alertes._periodicite({"recurring": {"interval": "year", "interval_count": 1}}) == "/ an",
             "annuel")
    verifier(alertes._periodicite({"recurring": {"interval": "year", "interval_count": 2}}) == "/ 2 ans",
             "pluriel : '2 ans', pas '2 an'")
    verifier(alertes._periodicite({"recurring": {"interval": "day", "interval_count": 10}}) == "/ 10 jours",
             "pluriel : '10 jours'")
    verifier(alertes._periodicite({"recurring": {"interval": "month", "interval_count": 6}}) == "/ 6 mois",
             "'mois' est invariable")
    verifier(alertes._periodicite({}) is None, "prix non récurrent -> None")


# --- évènements ----------------------------------------------------------------

def _evenement(type_evenement: str, objet: dict, livemode: bool = True, event_id: str = "evt_1") -> dict:
    return {
        "id": event_id,
        "type": type_evenement,
        "livemode": livemode,
        "data": {"object": objet},
    }


ABONNEMENT = {
    "id": "sub_123",
    "customer": {"id": "cus_1", "name": "Marie Dupont", "email": "marie@exemple.fr"},
    "status": "active",
    "start_date": 1789084800,   # 11/09/2026
    "items": {"data": [{
        "quantity": 1,
        "price": {
            "id": "price_1", "nickname": "Formule Premium", "unit_amount": 2900,
            "currency": "eur", "recurring": {"interval": "month", "interval_count": 1},
        },
    }]},
}


def test_abonnement_cree() -> None:
    print("\nAlerte : abonnement créé")
    texte = alertes.formater(_evenement("customer.subscription.created", ABONNEMENT))
    verifier("Nouvel abonnement" in texte, "titre présent")
    verifier("Marie Dupont — marie@exemple.fr" in texte, "nom et email du client")
    verifier("Formule Premium" in texte, "nom de la formule")
    verifier("29,00 €" in texte, "montant")
    verifier("/ mois" in texte, "périodicité")
    verifier("11/09/2026" in texte, "date de début en format français")
    verifier("dashboard.stripe.com/subscriptions/sub_123" in texte, "lien vers Stripe")
    verifier("/test/" not in texte, "mode réel : pas de préfixe /test/ dans le lien")


def test_mode_test_signale() -> None:
    print("\nAlerte : mode test Stripe")
    texte = alertes.formater(
        _evenement("customer.subscription.created", ABONNEMENT, livemode=False)
    )
    verifier(texte.startswith("🧪"), "le message de test est signalé dès la première ligne")
    verifier("mode test Stripe" in texte, "le message dit qu'il vient du mode test")
    verifier("dashboard.stripe.com/test/subscriptions/sub_123" in texte,
             "lien vers le dashboard en mode test")


def test_quantite_et_total() -> None:
    print("\nAlerte : plusieurs lignes et quantités")
    abo = json.loads(json.dumps(ABONNEMENT))
    abo["items"]["data"][0]["quantity"] = 3
    abo["items"]["data"].append({
        "quantity": 1,
        "price": {"id": "price_2", "nickname": "Option SAV", "unit_amount": 1000,
                  "currency": "eur", "recurring": {"interval": "month", "interval_count": 1}},
    })
    texte = alertes.formater(_evenement("customer.subscription.created", abo))
    verifier("×3" in texte, "quantité affichée")
    verifier("87,00 €" in texte, "3 × 29,00 € = 87,00 € sur la ligne")
    verifier("<b>Total : 97,00 € / mois</b>" in texte, "total = 87 + 10 = 97,00 €")


def test_prix_variable_pas_de_faux_total() -> None:
    print("\nAlerte : tarification à l'usage")
    abo = json.loads(json.dumps(ABONNEMENT))
    abo["items"]["data"][0]["price"]["unit_amount"] = None   # prix à paliers / à l'usage
    texte = alertes.formater(_evenement("customer.subscription.created", abo))
    verifier("montant variable" in texte, "la ligne dit que le montant est variable")
    verifier("<b>Total" not in texte, "aucun total inventé quand un montant est inconnu")


def test_periode_essai() -> None:
    print("\nAlerte : période d'essai")
    abo = json.loads(json.dumps(ABONNEMENT))
    abo["trial_end"] = 1790294400
    texte = alertes.formater(_evenement("customer.subscription.created", abo))
    verifier("essai" in texte.lower(), "la période d'essai est signalée")


def test_incomplete_pas_dalerte() -> None:
    print("\nAbonnement créé mais paiement non abouti")
    for statut in ("incomplete", "incomplete_expired", "past_due", "unpaid"):
        abo = json.loads(json.dumps(ABONNEMENT))
        abo["status"] = statut
        verifier(alertes.formater(_evenement("customer.subscription.created", abo)) is None,
                 f"statut '{statut}' -> aucune alerte")


def test_essai_declenche_alerte() -> None:
    print("\nAbonnement démarré en période d'essai")
    abo = json.loads(json.dumps(ABONNEMENT))
    abo["status"] = "trialing"
    verifier(alertes.formater(_evenement("customer.subscription.created", abo)) is not None,
             "statut 'trialing' -> alerte (la souscription a bien eu lieu)")


def _mise_a_jour(statut_avant: str, statut_apres: str) -> dict:
    abo = json.loads(json.dumps(ABONNEMENT))
    abo["status"] = statut_apres
    return {
        "id": "evt_maj", "type": "customer.subscription.updated", "livemode": True,
        "data": {"object": abo, "previous_attributes": {"status": statut_avant}},
    }


def test_paiement_abouti_en_retard() -> None:
    print("\nPaiement initial abouti avec retard")
    message = alertes.formater(_mise_a_jour("incomplete", "active"))
    verifier(message is not None, "incomplete -> active : alerte de nouvel abonnement")
    verifier("Nouvel abonnement" in message, "c'est bien le message de souscription")
    verifier(alertes.formater(_mise_a_jour("incomplete", "trialing")) is not None,
             "incomplete -> trialing : alerte aussi")


def test_pas_dalerte_sur_les_autres_changements() -> None:
    print("\nAutres changements d'abonnement : silence")
    verifier(alertes.formater(_mise_a_jour("past_due", "active")) is None,
             "past_due -> active : reprise d'un abonnement existant, pas une souscription")
    verifier(alertes.formater(_mise_a_jour("trialing", "active")) is None,
             "fin d'essai : déjà annoncé à la souscription, pas de doublon")
    verifier(alertes.formater(_mise_a_jour("active", "canceled")) is None,
             "annulation : hors périmètre, aucune alerte")
    verifier(alertes.formater(_mise_a_jour("incomplete", "incomplete_expired")) is None,
             "paiement définitivement échoué : aucune alerte")
    maj = _mise_a_jour("incomplete", "active")
    maj["data"]["previous_attributes"] = {"default_payment_method": "pm_ancien"}
    verifier(alertes.formater(maj) is None,
             "changement de carte sans changement de statut : aucune alerte")


def test_quantite_zero() -> None:
    print("\nQuantité nulle")
    abo = json.loads(json.dumps(ABONNEMENT))
    abo["items"]["data"][0]["quantity"] = 0
    texte = alertes.formater(_evenement("customer.subscription.created", abo))
    verifier("×0" in texte, "la quantité 0 est visible")
    verifier("29,00" not in texte, "une quantité 0 n'annonce pas un revenu de 29 €")


def test_facturation_a_lusage() -> None:
    print("\nTarification à l'usage (metered)")
    abo = json.loads(json.dumps(ABONNEMENT))
    item = abo["items"]["data"][0]
    item["price"]["unit_amount"] = 5
    item["price"]["recurring"]["usage_type"] = "metered"
    item.pop("quantity")
    texte = alertes.formater(_evenement("customer.subscription.created", abo))
    verifier("par unité" in texte, "le prix est annoncé comme unitaire")
    verifier("<b>Total" not in texte, "aucun total mensuel inventé sur du facturé à l'usage")


def test_remise() -> None:
    print("\nAbonnement avec remise")
    abo = json.loads(json.dumps(ABONNEMENT))
    abo["discounts"] = [{"id": "di_1", "coupon": {"percent_off": 50}}]
    texte = alertes.formater(_evenement("customer.subscription.created", abo))
    verifier("<b>Total avant remise" in texte, "le total est étiqueté 'avant remise'")
    verifier("remise s'applique" in texte, "l'alerte prévient qu'une remise s'applique")
    texte = alertes.formater(_evenement("customer.subscription.created", ABONNEMENT))
    verifier("<b>Total : 29,00" in texte, "sans remise, le total reste un total simple")


def test_html_hostile_neutralise() -> None:
    print("\nNom de client hostile")
    abo = json.loads(json.dumps(ABONNEMENT))
    abo["customer"] = {"id": "cus_1",
                       "name": '<a href="https://piege.example">Payer ici</a>',
                       "email": "a@b.fr"}
    texte = alertes.formater(_evenement("customer.subscription.created", abo))
    verifier('<a href="https://piege.example"' not in texte,
             "le faux lien n'est pas rendu cliquable")
    verifier("&lt;a href=" in texte, "il est affiché littéralement")
    verifier('<a href="https://dashboard.stripe.com' in texte,
             "notre propre lien Stripe, lui, reste actif")

    abo["customer"]["name"] = "Marie\n<b>Total : 9 999,00 € / mois</b>"
    texte = alertes.formater(_evenement("customer.subscription.created", abo))
    verifier("9 999,00" in texte, "le texte du nom reste visible")
    verifier("<b>Total : 9 999" not in texte,
             "mais il ne fabrique pas un faux total en gras")
    verifier(len([l for l in texte.split("\n") if "9 999" in l]) == 1
             and "Marie" in [l for l in texte.split("\n") if "9 999" in l][0],
             "et il ne fabrique pas non plus une ligne à lui tout seul")


def test_identifiant_produit_personnalise() -> None:
    """Stripe laisse choisir l'identifiant d'un produit : ceux d'ID by Rivoli sont
    'rivoli_standard', 'rivoli_pro'... Exiger un préfixe 'prod_' faisait afficher
    « formule inconnue » sur les abonnements les plus courants."""
    print("\nIdentifiant de produit personnalisé")
    appels: list = []

    class FauxProduit:
        @staticmethod
        def retrieve(identifiant):
            appels.append(identifiant)
            class R:
                @staticmethod
                def to_dict():
                    return {"name": "ID by Rivoli — Standard"}
            return R

    vrai_produit, vraie_cle = alertes.stripe.Product, alertes.stripe.api_key
    alertes.stripe.Product, alertes.stripe.api_key = FauxProduit, "rk_factice"
    try:
        verifier(alertes._nom_formule({"product": "rivoli_standard"}) == "ID by Rivoli — Standard",
                 "'rivoli_standard' est bien résolu en nom lisible")
        verifier(appels == ["rivoli_standard"], "le produit a bien été interrogé")
        verifier(alertes._nom_formule({"product": "prod_Tuz02UoxcfkqXy"}) == "ID by Rivoli — Standard",
                 "un identifiant classique 'prod_' marche toujours")
        verifier(alertes._nom_formule({"nickname": "Surnom", "product": "rivoli_pro"}) == "Surnom",
                 "un surnom de prix reste prioritaire, sans appel réseau")
    finally:
        alertes.stripe.Product, alertes.stripe.api_key = vrai_produit, vraie_cle


def test_cle_posee_par_le_module() -> None:
    """L'enrichissement doit marcher sans passer par le serveur : un outil en ligne
    de commande qui importe seulement `alertes` affichait des identifiants bruts
    tant que la clé était posée dans serveur.py."""
    print("\nClé Stripe portée par le module qui l'utilise")
    import subprocess
    code = (
        "import sys; sys.path.insert(0, '.');"
        "from app import alertes;"
        "import stripe;"
        "print('CLE_VUE' if stripe.api_key == 'rk_test_factice' else 'CLE_ABSENTE')"
    )
    env = dict(os.environ, STRIPE_API_KEY="rk_test_factice")
    sortie = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(RACINE), env=env).stdout
    verifier("CLE_VUE" in sortie,
             "importer `alertes` seul suffit à charger la clé Stripe")


def test_evenement_non_gere() -> None:
    print("\nÉvènement inconnu")
    verifier(alertes.formater(_evenement("charge.refunded", {})) is None,
             "un évènement sans mise en forme renvoie None")


def test_client_non_enrichi() -> None:
    print("\nClient sans enrichissement (pas de clé API)")
    abo = json.loads(json.dumps(ABONNEMENT))
    abo["customer"] = "cus_999"
    texte = alertes.formater(_evenement("customer.subscription.created", abo))
    verifier("cus_999" in texte, "l'identifiant brut est affiché, sans appel réseau")


# --- journal anti-doublon ------------------------------------------------------

def test_journal() -> None:
    print("\nJournal anti-doublon")
    chemin = Path(tempfile.mkdtemp()) / "journal.db"
    journal.initialiser(chemin)
    verifier(not journal.deja_traite(chemin, "evt_x"), "évènement inconnu au départ")
    journal.marquer_traite(chemin, "evt_x", "customer.subscription.created")
    verifier(journal.deja_traite(chemin, "evt_x"), "évènement retenu après marquage")
    journal.marquer_traite(chemin, "evt_x", "customer.subscription.created")
    verifier(journal.deja_traite(chemin, "evt_x"), "marquer deux fois ne casse rien")


# --- bout en bout : la vraie route HTTP ----------------------------------------

def _signer(corps: bytes, secret: str, horodatage: int | None = None) -> str:
    """Reproduit exactement l'en-tête Stripe-Signature envoyé par Stripe."""
    t = horodatage if horodatage is not None else int(time.time())
    signature = hmac.new(
        secret.encode(), f"{t}.".encode() + corps, hashlib.sha256
    ).hexdigest()
    return f"t={t},v1={signature}"


def test_route_webhook() -> None:
    print("\nRoute /webhook/stripe")
    envois: list = []
    vrai_envoyer = telegram.envoyer
    telegram_module = sys.modules["app.telegram"]
    serveur_module = sys.modules["app.serveur"]

    def faux_envoyer(jeton, chat_id, texte):
        envois.append(texte)

    serveur_module.telegram.envoyer = faux_envoyer
    try:
        with TestClient(app) as client:
            corps = json.dumps(
                _evenement("customer.subscription.created", ABONNEMENT, event_id="evt_bout_en_bout")
            ).encode()

            # 1. Signature valide -> alerte envoyée
            r = client.post("/webhook/stripe", content=corps,
                            headers={"stripe-signature": _signer(corps, SECRET_TEST)})
            verifier(r.status_code == 200, "signature valide -> 200")
            verifier(len(envois) == 1, "une alerte envoyée sur Telegram")
            verifier("Marie Dupont" in envois[0], "l'alerte contient le bon client")

            # 2. Le même évènement rejoué -> pas de seconde alerte
            r = client.post("/webhook/stripe", content=corps,
                            headers={"stripe-signature": _signer(corps, SECRET_TEST)})
            verifier(r.status_code == 200, "rejeu -> 200")
            verifier(len(envois) == 1, "rejeu : aucune seconde alerte (dédoublonnage)")

            # 3. Mauvais secret -> refusé
            r = client.post("/webhook/stripe", content=corps,
                            headers={"stripe-signature": _signer(corps, "whsec_mauvais")})
            verifier(r.status_code == 400, "signature signée avec un autre secret -> 400")

            # 4. Aucune signature -> refusé
            r = client.post("/webhook/stripe", content=corps)
            verifier(r.status_code == 400, "sans en-tête de signature -> 400")

            # 5. Corps modifié après signature -> refusé
            sig = _signer(corps, SECRET_TEST)
            falsifie = corps.replace(b"Marie Dupont", b"Pirate XXXX")
            r = client.post("/webhook/stripe", content=falsifie,
                            headers={"stripe-signature": sig})
            verifier(r.status_code == 400, "corps falsifié après signature -> 400")

            # 6. Signature trop vieille (rejeu d'un ancien message) -> refusé
            vieux = int(time.time()) - 3600
            r = client.post("/webhook/stripe", content=corps,
                            headers={"stripe-signature": _signer(corps, SECRET_TEST, vieux)})
            verifier(r.status_code == 400, "signature vieille d'une heure -> 400")

            # 7. Évènement non surveillé -> acquitté sans alerte
            autre = json.dumps(_evenement("charge.refunded", {}, event_id="evt_autre")).encode()
            r = client.post("/webhook/stripe", content=autre,
                            headers={"stripe-signature": _signer(autre, SECRET_TEST)})
            verifier(r.status_code == 200, "évènement non surveillé -> 200")
            verifier(len(envois) == 1, "évènement non surveillé : aucune alerte")

            # 8. Telegram en panne passagère -> 503 pour que Stripe rejoue
            def envoyer_ko(jeton, chat_id, texte):
                raise telegram_module.TelegramErreurTemporaire("panne simulée")

            serveur_module.telegram.envoyer = envoyer_ko
            panne = json.dumps(
                _evenement("customer.subscription.created", ABONNEMENT, event_id="evt_panne")
            ).encode()
            r = client.post("/webhook/stripe", content=panne,
                            headers={"stripe-signature": _signer(panne, SECRET_TEST)})
            verifier(r.status_code == 503, "Telegram en panne -> 503 (Stripe rejouera)")
            verifier(not journal.deja_traite(config.DB_PATH, "evt_panne"),
                     "évènement non marqué traité : l'alerte n'est pas perdue")

            # 9. Une fois Telegram revenu, le rejeu Stripe passe
            serveur_module.telegram.envoyer = faux_envoyer
            r = client.post("/webhook/stripe", content=panne,
                            headers={"stripe-signature": _signer(panne, SECRET_TEST)})
            verifier(r.status_code == 200, "rejeu après rétablissement -> 200")
            verifier(len(envois) == 2, "l'alerte retardée finit par partir")

            # 10. /health
            r = client.get("/health")
            verifier(r.status_code == 200 and r.json()["status"] == "ok", "/health répond ok")
    finally:
        serveur_module.telegram.envoyer = vrai_envoyer


def test_configuration_incomplete() -> None:
    """Une variable mal collée ne doit pas faire disparaître une vraie souscription."""
    print("\nConfiguration incomplète")
    serveur_module = sys.modules["app.serveur"]
    envois: list = []
    vrai_envoyer = serveur_module.telegram.envoyer
    serveur_module.telegram.envoyer = lambda *a: envois.append(a)
    jeton_valide = config.TELEGRAM_BOT_TOKEN
    config.TELEGRAM_BOT_TOKEN = ""         # simule une variable oubliée sur Railway
    try:
        with TestClient(app) as client:
            corps = json.dumps(
                _evenement("customer.subscription.created", ABONNEMENT, event_id="evt_sans_url")
            ).encode()
            r = client.post("/webhook/stripe", content=corps,
                            headers={"stripe-signature": _signer(corps, SECRET_TEST)})
            verifier(r.status_code == 503,
                     "TELEGRAM_BOT_TOKEN vide -> 503, pas 200 : Stripe rejouera")
            verifier(not journal.deja_traite(config.DB_PATH, "evt_sans_url"),
                     "l'évènement reste à traiter, l'alerte n'est pas perdue")
            verifier(len(envois) == 0, "aucun envoi tenté sans jeton")
            sante = client.get("/health")
            verifier(sante.status_code == 200,
                     "/health reste 200 : sinon Railway tue le service et Stripe "
                     "tombe sur une adresse morte au lieu d'un 503 rejouable")
            verifier(sante.json()["status"] == "degraded",
                     "mais il dit clairement que la configuration est incomplète")
            verifier("TELEGRAM_BOT_TOKEN" in sante.json()["configuration_manquante"],
                     "et nomme la variable à corriger")
    finally:
        config.TELEGRAM_BOT_TOKEN = jeton_valide
        serveur_module.telegram.envoyer = vrai_envoyer


def test_requetes_simultanees() -> None:
    """Trois évènements en même temps ne doivent pas s'attendre les uns les autres :
    Telegram et Stripe sont des appels bloquants, s'ils tournaient dans la boucle
    asyncio le serveur entier figerait (et Stripe rejouerait des évènements encore
    en cours de traitement, donc des doublons)."""
    print("\nTrois évènements simultanés")
    import asyncio
    import httpx

    serveur_module = sys.modules["app.serveur"]
    vrai_envoyer = serveur_module.telegram.envoyer

    def envoyer_lent(jeton, chat_id, texte):
        time.sleep(0.5)                     # bloquant, comme un vrai appel réseau

    serveur_module.telegram.envoyer = envoyer_lent

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async def un(n: int):
                corps = json.dumps(
                    _evenement("customer.subscription.created", ABONNEMENT, event_id=f"evt_par{n}")
                ).encode()
                return await client.post("/webhook/stripe", content=corps,
                                         headers={"stripe-signature": _signer(corps, SECRET_TEST)})
            debut = time.monotonic()
            reponses = await asyncio.gather(*(un(n) for n in range(3)))
            return time.monotonic() - debut, reponses

    try:
        journal.initialiser(config.DB_PATH)
        duree, reponses = asyncio.run(scenario())
    finally:
        serveur_module.telegram.envoyer = vrai_envoyer

    verifier(all(r.status_code == 200 for r in reponses), "les trois évènements aboutissent")
    verifier(duree < 1.2,
             f"traités en parallèle ({duree:.2f}s, et non ~1,5s en file d'attente)")



# --- compteurs et récapitulatifs ------------------------------------------------

def _base_neuve() -> Path:
    chemin = Path(tempfile.mkdtemp()) / "compteurs.db"
    journal.initialiser(chemin)
    return chemin


def _souscrire(db, event_id, montant, periodicite="/ mois", jour="2026-09-11", devise="eur"):
    journal.enregistrer_souscription(db, event_id, "sub_" + event_id, "cus_1",
                                     montant, devise, periodicite, jour)


def test_totaux() -> None:
    print("\nTotaux du jour")
    db = _base_neuve()
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(t["nombre"] == 0 and not t["groupes"], "journée vide")

    _souscrire(db, "e1", 1900)
    _souscrire(db, "e2", 2900)
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(t["nombre"] == 2, "deux souscriptions comptées")
    verifier(t["groupes"]["eur"] == 4800, "19,00 € + 29,00 € = 48,00 €")

    _souscrire(db, "e2", 9999)
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(t["nombre"] == 2, "le même évènement rejoué ne recompte pas")

    _souscrire(db, "e3", 1000, jour="2026-09-10")
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(t["nombre"] == 2, "la veille n'entre pas dans le total du jour")
    t = journal.totaux(db, "2026-09-05", "2026-09-11")
    verifier(t["nombre"] == 3, "elle entre dans les 7 derniers jours")


def test_devises_jamais_melangees() -> None:
    """Le volume additionne toutes les périodicités d'une même devise — un annuel à
    190 € rapporte bien 190 € le jour où il est souscrit — mais jamais deux devises :
    19 € et 19 $ ne font pas 38."""
    print("\nVolume par devise")
    db = _base_neuve()
    _souscrire(db, "m1", 1900, "/ mois")
    _souscrire(db, "a1", 19000, "/ an")
    _souscrire(db, "u1", 5000, "/ mois", devise="usd")
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(t["groupes"]["eur"] == 20900,
             "mensuel et annuel d'une même devise s'additionnent (19 + 190 = 209 €)")
    verifier(t["groupes"]["usd"] == 5000, "les dollars restent à part")
    lignes = alertes._formuler_groupes(t["groupes"])
    verifier(lignes == ["209,00 €", "50,00 $"], "un montant par devise, sans périodicité")


def test_souscription_non_chiffrable() -> None:
    print("\nSouscription non chiffrable")
    db = _base_neuve()
    _souscrire(db, "c1", 1900)
    _souscrire(db, "c2", None, None)
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(t["nombre"] == 2, "elle est bien comptée")
    verifier(t["groupes"]["eur"] == 1900, "mais pas ajoutée au montant")
    verifier(t["non_chiffrables"] == 1, "elle est signalée à part")
    texte = alertes.message_point_du_jour(16, t)
    verifier("à l'usage" in texte, "le message prévient que le montant est incomplet")
    verifier(len(texte.split("\n")) == 2,
             "titre + réserve, sans répéter les chiffres une seconde fois")


def test_phrase_cumul() -> None:
    print("\nCumul affiché dans l'alerte")
    db = _base_neuve()
    _souscrire(db, "p1", 1900)
    phrase = alertes.phrase_cumul(journal.totaux(db, "2026-09-11", "2026-09-11"), {"eur": 344254})
    verifier("1re souscription" in phrase, "la première dit '1re'")
    verifier("3 442,54 € net" in phrase, "avec le volume net du jour, comme les récapitulatifs")
    verifier("19,00" not in phrase, "et plus le montant catalogue")
    _souscrire(db, "p2", 2900)
    phrase = alertes.phrase_cumul(journal.totaux(db, "2026-09-11", "2026-09-11"), None)
    verifier(phrase == "2e souscription du jour",
             "volume inconnu : le titre ne dit que le compte, sans annoncer zéro")
    verifier("<" not in phrase,
             "le cumul est un titre, pas du HTML : il sera mis en gras par _bloc")


def test_messages_recap() -> None:
    print("\nMessages de récapitulatif")
    db = _base_neuve()
    vide = journal.totaux(db, "2026-09-11", "2026-09-11")
    texte = alertes.message_point_du_jour(16, vide)
    verifier("16h" in texte, "l'heure du point est indiquée")
    verifier("aucune souscription" in texte.lower(), "une journée vide le dit clairement")
    verifier(texte.split("\n")[0].count("aucune souscription") == 1,
             "et le dit dès la première ligne, seule visible dans la notification")

    _souscrire(db, "r1", 1900)
    _souscrire(db, "r2", 2900)
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    texte = alertes.message_point_du_jour(20, t, {"eur": 332513})
    premiere = texte.split("\n")[0]
    verifier("2 nouvelles souscriptions" in premiere, "le nombre est sur la première ligne")
    verifier("3 325,13 € net" in premiere, "le volume net aussi : tout est lisible sans ouvrir")
    verifier(len(texte.split("\n")) == 1,
             "sans MRR ni réserve, le point du jour tient sur sa seule première ligne")

    _souscrire(db, "r3", 1000, jour="2026-09-08")
    jour = datetime(2026, 9, 11, tzinfo=alertes.FUSEAU)
    texte = alertes.message_bilan(
        jour, journal.totaux(db, "2026-09-11", "2026-09-11"),
        journal.totaux(db, "2026-09-05", "2026-09-11"),
    )
    verifier("vendredi 11 septembre" in texte.split("\n")[0], "le bilan nomme le jour")
    verifier("2 nouvelles souscriptions" in texte.split("\n")[0],
             "avec les chiffres de la journée dès la première ligne")
    verifier("7 derniers jours" in texte, "et la semaine dans le corps")
    verifier("3 nouvelles souscriptions" in texte, "la semaine inclut les jours précédents")


def _banc_recap(db, heures, instant):
    """Prépare le décor d'un test de récapitulatif et rend une fonction de nettoyage.
    Le MRR et le volume net sont remplacés par des valeurs fixes : aucun réseau."""
    etat = (recap.telegram.envoyer, recap._maintenant, config.RECAP_HEURES, config.DB_PATH,
            recap.abonnes.etat, recap.volume.net_sur_periode)
    envois: list = []
    recap.telegram.envoyer = lambda jeton, chat_id, texte: envois.append(texte)
    recap._maintenant = lambda: instant
    recap.abonnes.etat = lambda: {"mrr": {"eur": 100}, "mrr_eur": 100, "abonnes": 1}
    recap.volume.net_sur_periode = lambda a, b: {"eur": 0}
    config.RECAP_HEURES, config.DB_PATH = heures, db

    def nettoyer():
        (recap.telegram.envoyer, recap._maintenant, config.RECAP_HEURES, config.DB_PATH,
         recap.abonnes.etat, recap.volume.net_sur_periode) = etat

    return envois, nettoyer


def test_planification_recaps() -> None:
    print("\nDéclenchement des récapitulatifs")
    db = _base_neuve()
    def a(h, m=5):
        return datetime(2026, 9, 11, h, m, tzinfo=alertes.FUSEAU)
    envois, nettoyer = _banc_recap(db, [0, 12, 16, 18, 20, 22], a(8))
    try:
        recap.neutraliser_creneaux_passes()      # comme au démarrage du service
        verifier(recap.verifier_une_fois(a(11)) == [], "à 11h05, rien n'est dû")
        verifier(recap.verifier_une_fois(a(12)) == ["2026-09-11 12"],
                 "à 12h05, le point de 12h part")
        verifier(recap.verifier_une_fois(a(12, 30)) == [],
                 "il ne repart pas à 12h30 : un créneau n'est envoyé qu'une fois")
        verifier(recap.verifier_une_fois(a(16)) == ["2026-09-11 16"],
                 "celui de 16h part à son tour")

        # Service arrêté de 16h30 à 21h : 18h et 20h sont en retard.
        verifier(recap.verifier_une_fois(a(21)) == ["2026-09-11 20"],
                 "au retour, seul le point le plus récent est envoyé")
        verifier(journal.recap_deja_envoye(db, "2026-09-11 18"),
                 "celui de 18h est marqué sans être posté : il n'est plus d'actualité")
        verifier(len(envois) == 3, "trois messages en tout, pas cinq")
    finally:
        nettoyer()


def test_bilan_minuit_toujours_envoye() -> None:
    print("\nBilan de minuit en retard")
    db = _base_neuve()
    # Service arrêté le 10 à 23h, revenu le 11 à 13h : minuit ET 12h sont dus.
    envois, nettoyer = _banc_recap(
        db, [0, 12, 16], datetime(2026, 9, 10, 23, tzinfo=alertes.FUSEAU))
    try:
        recap.neutraliser_creneaux_passes()
        envoyes = recap.verifier_une_fois(datetime(2026, 9, 11, 13, tzinfo=alertes.FUSEAU))
        verifier("2026-09-11 0" in envoyes,
                 "le bilan de minuit part malgré le retard : c'est la trace du jour")
        verifier("2026-09-11 12" in envoyes, "le point de 12h aussi, c'est le plus récent")
        verifier(any("Bilan du jeudi 10 septembre" in t for t in envois),
                 "et il porte bien sur la journée écoulée, pas celle en cours")
    finally:
        nettoyer()


def test_coupure_de_plusieurs_jours() -> None:
    """Une panne de deux jours ne doit pas effacer les bilans quotidiens traversés :
    chacun est la seule trace chiffrée de sa journée."""
    print("\nCoupure de plusieurs jours")
    db = _base_neuve()
    envois, nettoyer = _banc_recap(
        db, [0, 12, 16, 18, 20, 22], datetime(2026, 9, 9, 23, tzinfo=alertes.FUSEAU))
    try:
        recap.neutraliser_creneaux_passes()
        envoyes = recap.verifier_une_fois(datetime(2026, 9, 12, 21, tzinfo=alertes.FUSEAU))
        bilans = [c for c in envoyes if c.endswith(" 0")]
        verifier(bilans == ["2026-09-10 0", "2026-09-11 0", "2026-09-12 0"],
                 "les trois bilans manqués sont rattrapés, un par journée")
        verifier(any("Bilan du mercredi 9 septembre" in t for t in envois),
                 "le bilan du 9 septembre part bien")
        verifier(any("Bilan du vendredi 11 septembre" in t for t in envois),
                 "celui du 11 aussi")
        verifier([c for c in envoyes if not c.endswith(" 0")] == ["2026-09-12 20"],
                 "mais un seul point du jour, le plus récent")
        anciens = [t for t in envois if "Bilan du mercredi 9" in t or "Bilan du jeudi 10" in t]
        verifier(anciens and all("MRR" not in t for t in anciens),
                 "les bilans rattrapés des jours passés ne portent pas le MRR d'aujourd'hui")
    finally:
        nettoyer()


def test_premier_demarrage_silencieux() -> None:
    print("\nPremier démarrage")
    db = _base_neuve()
    envois, nettoyer = _banc_recap(
        db, [0, 12, 16, 18, 20, 22], datetime(2026, 9, 11, 21, tzinfo=alertes.FUSEAU))
    try:
        recap.neutraliser_creneaux_passes()
        verifier(envois == [], "un déploiement à 21h n'envoie pas les 5 récaps de la journée")
        verifier(recap.verifier_une_fois(datetime(2026, 9, 11, 21, 30, tzinfo=alertes.FUSEAU)) == [],
                 "et ils ne repartent pas non plus juste après")
        verifier(recap.verifier_une_fois(datetime(2026, 9, 11, 22, 1, tzinfo=alertes.FUSEAU))
                 == ["2026-09-11 22"], "mais le créneau suivant fonctionne normalement")
    finally:
        nettoyer()


def test_mode_test_non_compte() -> None:
    print("\nLes abonnements de test ne comptent pas")
    db = _base_neuve()
    envois: list = []
    serveur_module = sys.modules["app.serveur"]
    vrai_envoyer, vrai_db = serveur_module.telegram.envoyer, config.DB_PATH
    serveur_module.telegram.envoyer = lambda jeton, chat_id, texte: envois.append(texte)
    config.DB_PATH = db
    try:
        with TestClient(app) as client:
            for n, livemode in ((1, False), (2, True)):
                corps = json.dumps(_evenement("customer.subscription.created", ABONNEMENT,
                                              livemode=livemode, event_id=f"evt_lm{n}")).encode()
                client.post("/webhook/stripe", content=corps,
                            headers={"stripe-signature": _signer(corps, SECRET_TEST)})
        jour = datetime.now(alertes.FUSEAU).date().isoformat()
        t = journal.totaux(db, jour, jour)
        verifier(t["nombre"] == 1, "seul l'abonnement réel est compté")
        verifier("journée en cours" in envois[0] or "aucune souscription réelle" in envois[0],
                 "l'alerte de test porte quand même l'état réel de la journée")
        verifier("1re souscription" not in envois[0],
                 "mais sans s'y compter")
        verifier("1re souscription" in envois[1].split("\n")[0],
                 "l'alerte réelle, elle, s'inclut dans le compte du jour")
    finally:
        serveur_module.telegram.envoyer, config.DB_PATH = vrai_envoyer, vrai_db


def test_cumul_sur_deux_souscriptions() -> None:
    print("\nCumul de bout en bout")
    db = _base_neuve()
    envois: list = []
    serveur_module = sys.modules["app.serveur"]
    vrai_envoyer, vrai_db = serveur_module.telegram.envoyer, config.DB_PATH
    serveur_module.telegram.envoyer = lambda jeton, chat_id, texte: envois.append(texte)
    config.DB_PATH = db
    try:
        with TestClient(app) as client:
            for n in (1, 2):
                corps = json.dumps(_evenement("customer.subscription.created", ABONNEMENT,
                                              event_id=f"evt_cumul{n}")).encode()
                client.post("/webhook/stripe", content=corps,
                            headers={"stripe-signature": _signer(corps, SECRET_TEST)})
        verifier(envois[0].split("\n")[0].startswith("<b>🎉 1re souscription"),
                 "la première annonce '1re' dès le titre")
        verifier("2e souscription" in envois[1].split("\n")[0], "la seconde annonce '2e'")
    finally:
        serveur_module.telegram.envoyer, config.DB_PATH = vrai_envoyer, vrai_db



def test_volume_lisible_en_une_ligne() -> None:
    """Regrouper par devise plutôt que par périodicité ramène cinq lignes illisibles
    à deux montants, qui tiennent dans le titre de la notification."""
    print("\nVolume tenant sur une ligne")
    db = _base_neuve()
    for i, (m, per, dev) in enumerate([(1900, "/ mois", "eur"), (2900, "/ 28 jours", "eur"),
                                       (3900, "/ mois", "usd"), (4900, "/ an", "usd"),
                                       (5900, "/ 28 jours", "usd")]):
        _souscrire(db, f"d{i}", m, per, devise=dev)
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(len(t["groupes"]) == 2, "cinq périodicités, mais deux devises : deux montants")
    verifier(alertes._formuler_groupes(t["groupes"]) == ["48,00 €", "147,00 $"],
             "un montant par devise")


class _FauxEvenement:
    def __init__(self, contenu):
        self._contenu = contenu

    def to_dict(self):
        return self._contenu


class _FauxEvenements:
    """Remplace l'accès Stripe du rattrapage pour ne jamais toucher le réseau."""
    appels: list = []
    contenu: list = []

    @classmethod
    def lister(cls, depuis):
        cls.appels.append(depuis)
        return iter([_FauxEvenement(e) for e in cls.contenu])


def test_rattrapage() -> None:
    """Au démarrage, les compteurs doivent refléter ce qui s'est déjà passé : sinon
    la première alerte annonce « 1re souscription aujourd'hui » sur une journée qui
    en compte déjà trente."""
    print("\nRattrapage de l'historique Stripe")
    db = _base_neuve()
    jour = datetime.now(alertes.FUSEAU).date().isoformat()
    instant = int(datetime.now(alertes.FUSEAU).timestamp())

    def evt(eid, statut="active", livemode=True, montant=1900):
        abo = json.loads(json.dumps(ABONNEMENT))
        abo["status"] = statut
        abo["items"]["data"][0]["price"]["unit_amount"] = montant
        return {"id": eid, "type": "customer.subscription.created", "livemode": livemode,
                "created": instant, "data": {"object": abo}}

    _FauxEvenements.contenu = [
        evt("evt_r1"), evt("evt_r2", montant=2900),
        evt("evt_r3", statut="incomplete"),      # carte refusée : ne compte pas
        evt("evt_r4", livemode=False),           # mode test : ne compte pas
    ]
    _FauxEvenements.appels = []

    envois: list = []
    vrai_lister, vrai_db = rattrapage._lister_evenements, config.DB_PATH
    vrai_envoyer, vraie_cle = telegram.envoyer, config.STRIPE_API_KEY
    rattrapage._lister_evenements = _FauxEvenements.lister
    config.STRIPE_API_KEY = "rk_factice"
    config.DB_PATH = db
    telegram.envoyer = lambda *a: envois.append(a)
    try:
        ajoutes = rattrapage.executer()
        verifier(ajoutes == 2, "seules les deux souscriptions réelles et abouties sont reprises")
        verifier(envois == [], "le rattrapage n'envoie AUCUNE alerte : c'est du passé")

        t = journal.totaux(db, jour, jour)
        verifier(t["nombre"] == 2, "elles sont comptées dans la journée")
        verifier(t["groupes"]["eur"] == 4800, "avec leurs montants (19 + 29 €)")

        verifier(alertes.phrase_cumul({**t, "nombre": t["nombre"] + 1}, None).startswith("3e"),
                 "la prochaine alerte annoncera donc '3e souscription', pas '1re'")

        ajoutes = rattrapage.executer()
        verifier(journal.totaux(db, jour, jour)["nombre"] == 2,
                 "relancer le rattrapage ne compte jamais deux fois la même souscription")
        verifier(ajoutes == 0,
                 "et il annonce 0 ajout, pas 2 : le décompte porte sur les lignes "
                 "réellement créées, pas sur les évènements examinés")

        verifier(rattrapage.ETAT["execute"] and rattrapage.ETAT["probleme"] is None,
                 "l'état du rattrapage est exposé, pour que /health puisse le dire")

        verifier(_FauxEvenements.appels[0].hour == 0 and _FauxEvenements.appels[0].minute == 0,
                 "la fenêtre relue commence à minuit, pas à l'heure courante")
    finally:
        rattrapage._lister_evenements, config.DB_PATH = vrai_lister, vrai_db
        telegram.envoyer, config.STRIPE_API_KEY = vrai_envoyer, vraie_cle


def test_rattrapage_sans_appels_de_libelles() -> None:
    """Relire une semaine d'historique ne doit pas déclencher un appel réseau par
    ligne d'abonnement : le rattrapage n'affiche jamais les noms de formules."""
    print("\nRattrapage sans résolution des noms")
    appels: list = []

    class ProduitEspion:
        @staticmethod
        def retrieve(identifiant):
            appels.append(identifiant)
            raise AssertionError("le rattrapage ne doit pas interroger les produits")

    abo = json.loads(json.dumps(ABONNEMENT))
    abo["items"]["data"][0]["price"] = {"product": "rivoli_standard", "unit_amount": 1900,
                                        "currency": "eur",
                                        "recurring": {"interval": "month", "interval_count": 1}}
    vrai_produit, vraie_cle = alertes.stripe.Product, alertes.stripe.api_key
    alertes.stripe.Product, alertes.stripe.api_key = ProduitEspion, "rk_factice"
    try:
        montant, devise, periodicite, partiel = alertes.resume_chiffre_sans_reseau(abo)
        verifier(appels == [], "aucun appel à Stripe pour résoudre le nom du produit")
        verifier(montant == 1900 and devise == "eur" and periodicite == "/ mois",
                 "les chiffres sont pourtant tous corrects")
    finally:
        alertes.stripe.Product, alertes.stripe.api_key = vrai_produit, vraie_cle


def test_rattrapage_sans_cle() -> None:
    print("\nRattrapage sans clé Stripe")
    vraie_cle = config.STRIPE_API_KEY
    config.STRIPE_API_KEY = ""
    try:
        verifier(rattrapage.executer() == 0,
                 "sans clé, le rattrapage s'abstient au lieu d'échouer")
    finally:
        config.STRIPE_API_KEY = vraie_cle


def test_rattrapage_jamais_bloquant() -> None:
    """Une pipeline qui alerte avec des compteurs incomplets vaut mieux qu'une
    pipeline qui refuse de démarrer."""
    print("\nRattrapage en panne")
    db = _base_neuve()
    vrai_lister, vrai_db = rattrapage._lister_evenements, config.DB_PATH
    vraie_cle = config.STRIPE_API_KEY
    config.STRIPE_API_KEY = "rk_factice"
    config.DB_PATH = db

    def lister_qui_echoue(depuis):
        raise RuntimeError("API Stripe indisponible")

    rattrapage._lister_evenements = lister_qui_echoue
    try:
        verifier(rattrapage.executer() == 0, "l'échec est absorbé, sans exception")
        verifier(rattrapage.ETAT["probleme"] is not None,
                 "mais le problème est signalé, pas passé sous silence")
    finally:
        rattrapage._lister_evenements, config.DB_PATH = vrai_lister, vrai_db
        config.STRIPE_API_KEY = vraie_cle



class _FauxMouvements:
    """Remplace l'accès aux mouvements du solde pour ne jamais toucher le réseau."""
    contenu: list = []
    erreur = None
    appels = 0

    @classmethod
    def lister(cls, debut, fin):
        cls.appels += 1
        if cls.erreur:
            raise cls.erreur
        return iter([_FauxEvenement(m) for m in cls.contenu])


def test_volume_net() -> None:
    """Le volume net ne compte que les ventes et les remboursements. Un virement
    vers la banque (payout) n'est PAS une perte : compté comme tel, il transformait
    3 325 € encaissés en 487 € — un chiffre faux de 85 %."""
    print("\nVolume net encaissé")
    _FauxMouvements.contenu = [
        {"type": "charge", "currency": "eur", "amount": 250000},
        {"type": "payment", "currency": "eur", "amount": 82513},
        {"type": "refund", "currency": "eur", "amount": -1900},
        {"type": "refund_failure", "currency": "eur", "amount": 500},  # remboursement raté : l'argent revient
        {"type": "payout", "currency": "eur", "amount": -279243},   # virement bancaire
        {"type": "stripe_fee", "currency": "eur", "amount": -4507}, # frais Stripe
    ]
    _FauxMouvements.erreur = None
    _FauxMouvements.appels = 0
    volume._cache.clear()
    vrai, vraie_cle = volume._lister_mouvements, config.STRIPE_API_KEY
    volume._lister_mouvements, config.STRIPE_API_KEY = _FauxMouvements.lister, "rk_factice"
    try:
        net = volume.net_de_la_journee(datetime.now(alertes.FUSEAU))
        verifier(net == {"eur": 331113},
                 "2 500 + 825,13 - 19 + 5 = 3 311,13 € : ni le virement ni les frais n'entrent, "
                 "et un remboursement raté revient dans le volume")
        volume.net_de_la_journee(datetime.now(alertes.FUSEAU))
        verifier(_FauxMouvements.appels == 1,
                 "la même période n'est pas relue dans les 5 minutes (panne Telegram)")

        verifier(alertes._volume_lisible(net) == "3 311,13 €", "le montant s'affiche bien")

        _FauxMouvements.contenu = []
        volume._cache.clear()
        vide = volume.net_de_la_journee(datetime.now(alertes.FUSEAU))
        verifier(alertes._volume_lisible(vide) == "0,00 €",
                 "une journée sans vente affiche 0,00 € — et non rien, qui voudrait "
                 "dire « on ne sait pas »")
    finally:
        volume._lister_mouvements, config.STRIPE_API_KEY = vrai, vraie_cle
        volume._cache.clear()


def test_volume_net_indisponible() -> None:
    """Sans la permission Stripe, le récapitulatif doit partir quand même — sans la
    ligne, plutôt qu'en annonçant zéro alors qu'on ne sait pas."""
    print("\nVolume net inaccessible")
    import stripe as _stripe
    vrai, vraie_cle = volume._lister_mouvements, config.STRIPE_API_KEY
    _FauxMouvements.erreur = _stripe.PermissionError("permission manquante")
    volume._lister_mouvements, config.STRIPE_API_KEY = _FauxMouvements.lister, "rk_factice"
    volume._cache.clear()
    try:
        verifier(volume.net_de_la_journee(datetime.now(alertes.FUSEAU)) is None,
                 "l'absence de permission renvoie None, sans exception")
        verifier("Balance transactions" in (volume.ETAT["probleme"] or ""),
                 "et /health saura dire quelle permission manque")
        verifier(alertes._volume_lisible(None) is None,
                 "et le volume disparaît du titre, sans annoncer zéro")
        t = {"nombre": 2, "groupes": {"eur": 4800}, "non_chiffrables": 0, "partiels": 0}
        verifier("2 nouvelles souscriptions" in alertes.message_point_du_jour(20, t, None),
                 "le récapitulatif part quand même, avec les souscriptions")
    finally:
        volume._lister_mouvements, config.STRIPE_API_KEY = vrai, vraie_cle
        _FauxMouvements.erreur = None



def test_deuxieme_ligne_mrr() -> None:
    print("\nDeuxième ligne : MRR et abonnés")
    t = {"nombre": 37, "groupes": {"eur": 92976}, "non_chiffrables": 0, "partiels": 0}
    etat = {"mrr": {"eur": 6636882, "usd": 7897713}, "mrr_eur": 12870692, "abonnes": 3738}
    texte = alertes.message_point_du_jour(20, t, {"eur": 332513}, etat)
    lignes = texte.split("\n")
    verifier("37 nouvelles souscriptions" in lignes[0] and "3 325,13 € net" in lignes[0],
             "1re ligne : souscriptions du jour et volume net")
    verifier("MRR ≈" in lignes[1] and "128 706,92 €" in lignes[1],
             "2e ligne : le MRR, marqué ≈ car recalculé")
    verifier("3738" in lignes[1] and "abonnés actifs" in lignes[1],
             "2e ligne : les abonnés actifs")
    verifier(alertes.ligne_indicateurs(None) == [],
             "sans accès aux abonnements, la 2e ligne disparaît simplement")

    sans_conversion = {"mrr": {"eur": 100, "gbp": 100}, "mrr_eur": None, "abonnes": 2}
    ligne = alertes.ligne_indicateurs(sans_conversion)[0]
    verifier("£" in ligne and "€" in ligne,
             "une devise non convertible : le détail par devise plutôt que rien")


def test_mensualisation() -> None:
    """Les conventions de Stripe, vérifiées sur le vrai compte le 11/09/2026 : un
    abonnement de 28 jours compte pour un mois plein, sans prorata."""
    print("\nMensualisation")
    m = abonnes._mensualiser
    verifier(m(1900, "month", 1) == 1900, "mensuel : inchangé")
    verifier(m(19000, "year", 1) == 19000 / 12, "annuel : divisé par 12")
    verifier(m(5700, "month", 3) == 1900, "trimestriel : divisé par 3")
    verifier(m(1900, "day", 28) == 1900, "28 jours = un mois, comme Stripe")
    verifier(m(1900, "day", 30) == 1900, "30 jours aussi")
    verifier(abs(m(700, "day", 7) - 700 * 365 / 12 / 7) < 0.01, "7 jours : au prorata")
    verifier(abs(m(700, "week", 1) - 700 * 365 / 12 / 7) < 0.01, "hebdomadaire : idem")


def test_mrr_abonnement() -> None:
    print("\nMRR d'un abonnement")
    def abo(**extra):
        base = {"customer": "cus_1", "status": "active", "items": {"data": [{
            "quantity": 1, "price": {"unit_amount": 1900, "currency": "eur",
                                     "recurring": {"interval": "month", "interval_count": 1}}}]}}
        base.update(extra); return base

    verifier(abonnes._mrr_abonnement(abo())[:2] == ("eur", 1900.0), "cas simple")
    remise = abo(discounts=[{"coupon": {"percent_off": 50}}])
    verifier(abonnes._mrr_abonnement(remise)[1] == 950.0, "remise de 50 % (ancien format)")
    remise2 = abo(discounts=[{"source": {"type": "coupon", "coupon": {"percent_off": 50}}}])
    verifier(abonnes._mrr_abonnement(remise2)[1] == 950.0,
             "remise de 50 % au format 2026 (source.coupon) — celui du vrai compte")
    identifiants = abo(discounts=["di_123"])   # non développée : identifiant brut
    verifier(abonnes._mrr_abonnement(identifiants)[1] == 1900.0,
             "une remise non développée est ignorée au lieu de faire planter le calcul")
    expiree = abo(discounts=[{"end": 1, "source": {"coupon": {"percent_off": 50}}}])
    verifier(abonnes._mrr_abonnement(expiree)[1] == 1900.0, "une remise terminée ne compte plus")

    annuel = abo(discounts=[{"source": {"coupon": {"amount_off": 2000}}}])
    annuel["items"]["data"][0]["price"] = {"unit_amount": 19000, "currency": "eur",
                                           "recurring": {"interval": "year", "interval_count": 1}}
    verifier(abs(abonnes._mrr_abonnement(annuel)[1] - (19000 - 2000) / 12) < 0.01,
             "20 € de remise sur un annuel : 1,67 €/mois en moins, pas 20 €/mois")

    usage = abo(); usage["items"]["data"][0]["price"]["recurring"]["usage_type"] = "metered"
    verifier(abonnes._mrr_abonnement(usage)[1] == 0, "le facturé à l'usage n'a pas de MRR")
    paliers = abo(); paliers["items"]["data"][0]["price"]["unit_amount"] = None
    devise, mensuel, non_chiffre = abonnes._mrr_abonnement(paliers)
    verifier(mensuel == 0 and non_chiffre, "un prix à paliers est signalé comme non chiffré")


def test_mrr_en_euros() -> None:
    print("\nConversion du MRR en euros")
    vrai = config.TAUX_EUR_USD
    config.TAUX_EUR_USD = 1.16
    try:
        verifier(abonnes._en_euros({"eur": 11600, "usd": 11600}) == 21600,
                 "116 € + 116 $ = 116 + 100 = 216 € au taux 1,16")
        verifier(abonnes._en_euros({"eur": 100, "gbp": 100}) is None,
                 "une devise inconnue : pas de total plutôt qu'un total amputé")
    finally:
        config.TAUX_EUR_USD = vrai


def test_abonnes_actifs_sans_impayes() -> None:
    """Le tableau de bord Stripe ne compte pas les impayés parmi les abonnés
    actifs (3 701 contre 4 061 en les incluant, vérifié le 11/09/2026) — mais
    leur MRR, lui, est bien compté, conformément à la définition de Stripe."""
    print("\nAbonnés actifs et impayés")
    def prix(montant): return {"unit_amount": montant, "currency": "eur",
                               "recurring": {"interval": "month", "interval_count": 1}}
    contenu = {
        "active": [
            {"customer": "cus_a", "status": "active",
             "items": {"data": [{"quantity": 1, "price": prix(1000)}]}},
            {"customer": "cus_paliers", "status": "active",          # prix à paliers
             "items": {"data": [{"quantity": 1, "price": prix(None)}]}},
        ],
        "past_due": [
            {"customer": "cus_b", "status": "past_due",
             "items": {"data": [{"quantity": 1, "price": prix(1000)}]}},
        ],
    }
    def lister(statut):
        return iter([_FauxEvenement(c) for c in contenu[statut]])

    vrai, vraie_cle = abonnes._lister_abonnements, config.STRIPE_API_KEY
    abonnes._lister_abonnements, config.STRIPE_API_KEY = lister, "rk_factice"
    abonnes._cache.update(instant=0.0, valeur=None)
    try:
        e = abonnes.etat()
        verifier(e["mrr"]["eur"] == 2000, "le MRR compte l'actif ET l'impayé")
        verifier(e["abonnes"] == 2,
                 "les abonnés actifs : l'actif et celui à paliers (il paie), pas l'impayé")
        verifier(abonnes.ETAT["paliers_non_chiffres"] == 1,
                 "le prix à paliers non chiffré est signalé dans l'état")
    finally:
        abonnes._lister_abonnements, config.STRIPE_API_KEY = vrai, vraie_cle
        abonnes._cache.update(instant=0.0, valeur=None)



if __name__ == "__main__":
    for test in [
        test_montants, test_periodicite, test_abonnement_cree, test_mode_test_signale,
        test_quantite_et_total, test_prix_variable_pas_de_faux_total, test_periode_essai,
        test_incomplete_pas_dalerte, test_essai_declenche_alerte,
        test_paiement_abouti_en_retard, test_pas_dalerte_sur_les_autres_changements,
        test_quantite_zero, test_facturation_a_lusage, test_remise,
        test_html_hostile_neutralise, test_identifiant_produit_personnalise,
        test_cle_posee_par_le_module,
        test_evenement_non_gere, test_client_non_enrichi,
        test_journal, test_route_webhook, test_configuration_incomplete,
        test_requetes_simultanees, test_totaux, test_devises_jamais_melangees,
        test_souscription_non_chiffrable, test_phrase_cumul, test_messages_recap,
        test_planification_recaps, test_bilan_minuit_toujours_envoye,
        test_coupure_de_plusieurs_jours,
        test_premier_demarrage_silencieux, test_mode_test_non_compte,
        test_cumul_sur_deux_souscriptions, test_volume_lisible_en_une_ligne,
        test_volume_net, test_volume_net_indisponible, test_deuxieme_ligne_mrr,
        test_mensualisation, test_mrr_abonnement, test_mrr_en_euros,
        test_abonnes_actifs_sans_impayes,
        test_rattrapage, test_rattrapage_sans_appels_de_libelles, test_rattrapage_sans_cle, test_rattrapage_jamais_bloquant,
    ]:
        test()
    print(f"\n{reussis} vérifications passées.")
