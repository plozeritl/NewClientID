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

from app import alertes, config, journal, recap, telegram  # noqa: E402
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
    verifier(t["groupes"][("eur", "/ mois")] == 4800, "19,00 € + 29,00 € = 48,00 €")

    _souscrire(db, "e2", 9999)
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(t["nombre"] == 2, "le même évènement rejoué ne recompte pas")

    _souscrire(db, "e3", 1000, jour="2026-09-10")
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(t["nombre"] == 2, "la veille n'entre pas dans le total du jour")
    t = journal.totaux(db, "2026-09-05", "2026-09-11")
    verifier(t["nombre"] == 3, "elle entre dans les 7 derniers jours")


def test_mensuel_et_annuel_jamais_additionnes() -> None:
    print("\nMensuel et annuel")
    db = _base_neuve()
    _souscrire(db, "m1", 1900, "/ mois")
    _souscrire(db, "a1", 19000, "/ an")
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(len(t["groupes"]) == 2, "deux natures de revenu, deux groupes")
    lignes = alertes._formuler_groupes(t["groupes"])
    verifier(any("19,00 € / mois" in l for l in lignes), "le mensuel est annoncé à part")
    verifier(any("190,00 € / an" in l for l in lignes), "l'annuel aussi")
    verifier(not any("209" in l for l in lignes),
             "19 €/mois + 190 €/an ne font jamais 209 € : ce chiffre n'aurait aucun sens")


def test_souscription_non_chiffrable() -> None:
    print("\nSouscription non chiffrable")
    db = _base_neuve()
    _souscrire(db, "c1", 1900)
    _souscrire(db, "c2", None, None)
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    verifier(t["nombre"] == 2, "elle est bien comptée")
    verifier(t["groupes"][("eur", "/ mois")] == 1900, "mais pas ajoutée au montant")
    verifier(t["non_chiffrables"] == 1, "elle est signalée à part")
    texte = alertes.message_point_du_jour(16, t)
    verifier("à l'usage" in texte, "le message prévient que le montant est incomplet")


def test_phrase_cumul() -> None:
    print("\nCumul affiché dans l'alerte")
    db = _base_neuve()
    _souscrire(db, "p1", 1900)
    phrase = alertes.phrase_cumul(journal.totaux(db, "2026-09-11", "2026-09-11"))
    verifier("1re souscription" in phrase, "la première dit '1re'")
    verifier("19,00" not in phrase, "sans répéter le montant, déjà juste au-dessus")
    _souscrire(db, "p2", 2900)
    phrase = alertes.phrase_cumul(journal.totaux(db, "2026-09-11", "2026-09-11"))
    verifier("2e souscription" in phrase, "la deuxième dit '2e'")
    verifier("48,00 € / mois" in phrase, "et donne le cumul de la journée")


def test_messages_recap() -> None:
    print("\nMessages de récapitulatif")
    db = _base_neuve()
    vide = journal.totaux(db, "2026-09-11", "2026-09-11")
    texte = alertes.message_point_du_jour(16, vide)
    verifier("16h" in texte, "l'heure du point est indiquée")
    verifier("aucune souscription" in texte.lower(), "une journée vide le dit clairement")

    _souscrire(db, "r1", 1900)
    _souscrire(db, "r2", 2900)
    t = journal.totaux(db, "2026-09-11", "2026-09-11")
    texte = alertes.message_point_du_jour(20, t)
    verifier("2 nouvelles souscriptions" in texte, "le nombre est au pluriel")
    verifier("48,00 € / mois" in texte, "le montant cumulé y est")

    _souscrire(db, "r3", 1000, jour="2026-09-08")
    jour = datetime(2026, 9, 11, tzinfo=alertes.FUSEAU)
    texte = alertes.message_bilan(
        jour, journal.totaux(db, "2026-09-11", "2026-09-11"),
        journal.totaux(db, "2026-09-05", "2026-09-11"),
    )
    verifier("vendredi 11 septembre" in texte, "le bilan nomme le jour en français")
    verifier("Journée" in texte and "7 derniers jours" in texte, "les deux périodes y sont")
    verifier("3 nouvelles souscriptions" in texte, "la semaine inclut les jours précédents")


def _banc_recap(db, heures, instant):
    """Prépare le décor d'un test de récapitulatif et rend une fonction de nettoyage."""
    etat = (recap.telegram.envoyer, recap._maintenant, config.RECAP_HEURES, config.DB_PATH)
    envois: list = []
    recap.telegram.envoyer = lambda jeton, chat_id, texte: envois.append(texte)
    recap._maintenant = lambda: instant
    config.RECAP_HEURES, config.DB_PATH = heures, db

    def nettoyer():
        (recap.telegram.envoyer, recap._maintenant,
         config.RECAP_HEURES, config.DB_PATH) = etat

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
        verifier("Journée en cours" in envois[0],
                 "l'alerte de test porte quand même l'état de la journée")
        verifier("souscription aujourd'hui" not in envois[0],
                 "mais sans s'y compter : elle ne dit pas '1re souscription aujourd'hui'")
        verifier("1re souscription aujourd'hui" in envois[1],
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
        verifier("1re souscription aujourd'hui" in envois[0], "la première annonce '1re'")
        verifier("2e souscription aujourd'hui" in envois[1], "la seconde annonce '2e'")
        verifier("58,00 € / mois au total" in envois[1], "2 × 29,00 € = 58,00 € cumulés")
    finally:
        serveur_module.telegram.envoyer, config.DB_PATH = vrai_envoyer, vrai_db



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
        test_requetes_simultanees, test_totaux, test_mensuel_et_annuel_jamais_additionnes,
        test_souscription_non_chiffrable, test_phrase_cumul, test_messages_recap,
        test_planification_recaps, test_bilan_minuit_toujours_envoye,
        test_coupure_de_plusieurs_jours,
        test_premier_demarrage_silencieux, test_mode_test_non_compte,
        test_cumul_sur_deux_souscriptions,
    ]:
        test()
    print(f"\n{reussis} vérifications passées.")
