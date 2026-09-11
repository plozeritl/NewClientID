# Alertes Stripe → Telegram

Quand un client souscrit un abonnement Stripe, un message part dans un groupe
Telegram, dans la seconde. Des récapitulatifs chiffrés tombent ensuite à heures
fixes.

## Ce qui déclenche une alerte

**Une seule chose : une souscription qui aboutit.**

C'est moins évident qu'il n'y paraît. Stripe crée l'abonnement *avant* de savoir si
la carte passe : un paiement refusé produit quand même un abonnement, au statut
`incomplete`. Alerter bêtement sur « abonnement créé » annoncerait donc des clients
qui n'ont rien payé.

| Situation Stripe | Alerte ? |
|---|---|
| Abonnement créé, paiement passé (`active`) | **oui** |
| Abonnement créé avec période d'essai (`trialing`) | **oui** |
| Abonnement créé, carte refusée (`incomplete`) | non — on attend |
| Ce même abonnement, payé plus tard (3-D Secure, autre carte) | **oui**, à ce moment-là |
| Paiement d'un abonnement existant qui échoue | non |
| Annulation, fin d'abonnement | non |
| Changement de carte, de formule, fin d'essai | non |

Chaque souscription produit exactement une alerte, jamais deux.

## Les récapitulatifs

**Tout message porte le compte de la journée.** Sur une alerte réelle il s'inclut ;
sur une alerte du mode test, il se contente d'annoncer l'état réel sans s'y compter.

```
🎉 Nouvel abonnement
Camille Martin — camille.martin@exemple.fr
ID by Rivoli — Standard · 19,00 € / mois
Total : 19,00 € / mois
Démarré le 11/09/2026
— 3e souscription aujourd'hui · 48,00 € / mois au total
Voir dans Stripe
```

Et cinq points dans la journée, plus un bilan la nuit — tout à l'heure de Paris :

| Heure | Contenu |
|---|---|
| 12h, 16h, 18h, 20h, 22h | la journée en cours, depuis minuit |
| 00h | la journée écoulée + les 7 derniers jours |

```
📊 Point du jour — 18h
Depuis minuit : 3 nouvelles souscriptions · 190,00 € / an + 48,00 € / mois
```

```
🌙 Bilan du vendredi 11 septembre
Journée : 3 nouvelles souscriptions · 190,00 € / an + 48,00 € / mois

7 derniers jours : 4 nouvelles souscriptions · 190,00 € / an + 67,00 € / mois
```

Trois règles de comptage, pour que les chiffres veuillent dire quelque chose :

- **Le mensuel et l'annuel ne sont jamais additionnés.** 19 €/mois et 190 €/an ne
  font pas 209 € : ce sont deux natures de revenu différentes.
- **Les abonnements du mode test de Stripe ne comptent pas.** Vos essais ne
  polluent jamais les chiffres réels.
- **Un abonnement sans montant fixe** (tarification à paliers ou à l'usage) est
  compté mais pas chiffré — son montant n'existe qu'en fin de période. Le message le
  signale plutôt que de laisser croire à un total complet.

Une souscription est datée du **jour de l'évènement Stripe**, pas du moment où elle
est traitée : après une panne, les rattrapages retombent dans la bonne journée.

Le service peut redémarrer sans rien perdre. S'il était arrêté à 16h, le
récapitulatif manqué part au retour ; si plusieurs points du jour sont en retard, un
seul est envoyé (le plus récent). Les bilans de minuit, eux, partent tous — jusqu'à
3 jours en arrière — car chacun est la seule trace chiffrée de sa journée.

Pour changer les heures ou tout désactiver : `RECAP_HEURES` et `RECAP_ACTIF`.

## Mise en route

Cinq étapes. Les trois premières se font dans un navigateur ou dans Telegram.

### 1. Créer le bot Telegram

1. Dans Telegram, ouvrir une conversation avec **@BotFather**
2. Envoyer `/newbot`
3. Donner un nom, puis un identifiant finissant par `bot`
4. BotFather répond avec un **jeton** de la forme `123456:AAE...`

Ce jeton est un secret : qui l'a peut écrire partout où le bot est présent.
(`/revoke` auprès de BotFather en génère un nouveau si besoin.)

### 2. Créer le groupe et y ajouter le bot

Créer le groupe qui recevra les alertes, puis : nom du groupe → **Ajouter des
membres** → chercher le bot → l'ajouter.

### 3. Remplir le fichier .env

```bash
cp .env.example .env
```

Renseigner `TELEGRAM_BOT_TOKEN`, puis récupérer l'identifiant du groupe :

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python outils/trouver_chat_id.py
```

Le script liste les conversations connues du bot. Copier l'identifiant du groupe
(négatif, ex. `-1001234567890`) dans `TELEGRAM_CHAT_ID`.

*Si la liste est vide : le bot n'a pas encore « vu » le groupe. Écrire
`/start@votre_bot` dedans, puis relancer.*

### 4. Créer la clé Stripe (lecture seule) et vérifier

Dans Stripe : **Développeurs → Clés API → Créer une clé restreinte**, en choisissant
« Powering an integration you built ». Deux permissions, toutes les deux en **Read** :

- **Customers** → Read
- **Products** → Read

Elles servent uniquement à écrire « Marie Dupont — marie@exemple.fr » dans l'alerte
plutôt que « cus_123 ». Cette pipeline n'écrit jamais rien dans Stripe : lui donner
une clé standard `sk_` serait lui confier des droits dont elle n'a aucun usage.

La clé commence par `rk_` et n'est affichée qu'une fois. La coller dans
`STRIPE_API_KEY`, puis vérifier que tout répond :

```bash
./.venv/bin/python outils/tester_telegram.py
```

Une fausse alerte doit apparaître dans le groupe. Si elle n'arrive pas, c'est le
jeton ou l'identifiant de groupe qui est en cause — inutile d'aller plus loin avant
que ça marche.

### 5. Brancher Stripe

**En local, pour essayer** (nécessite la [CLI Stripe](https://docs.stripe.com/cli)) :

```bash
stripe listen --forward-to localhost:8000/webhook/stripe
```

La commande affiche une clé `whsec_…` : la mettre dans `.env` comme
`STRIPE_WEBHOOK_SECRET`. Puis, dans un autre terminal :

```bash
./.venv/bin/uvicorn app.serveur:app --port 8000
stripe trigger customer.subscription.created
```

**En production (Railway)** : déployer ce dossier, puis dans Stripe →
**Développeurs → Webhooks → Ajouter un endpoint** :

- URL : `https://<votre-service>.up.railway.app/webhook/stripe`
- Évènements : `customer.subscription.created` **et** `customer.subscription.updated`

Stripe affiche alors la clé secrète de l'endpoint (`whsec_…`) : la copier dans les
variables Railway comme `STRIPE_WEBHOOK_SECRET`.

⚠️ La clé de `stripe listen` et celle de l'endpoint du dashboard sont **différentes**.
Les confondre est la cause n°1 d'une pipeline muette.

⚠️ Monter un **volume sur `/data`** et poser `DB_PATH=/data/alertes.db`. C'est ce
fichier qui contient l'historique des souscriptions : sans volume, les totaux du jour
et de la semaine repartent de zéro à chaque redéploiement.

## Vérifier que tout est en place

`https://<votre-service>.up.railway.app/health` répond :

```json
{"status": "ok", "configuration_manquante": [], "enrichissement_stripe": true}
```

Si `status` vaut `degraded`, la liste `configuration_manquante` nomme la variable à
corriger. La réponse reste volontairement un code 200 : un 503 ferait échouer le
healthcheck de Railway, qui tuerait le service — et Stripe tomberait alors sur une
adresse morte au lieu du 503 qui déclenche le rejeu.

## Lancer les tests

```bash
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python tests/test_alertes.py
```

139 vérifications, sans aucun appel réseau : mise en forme des montants, statuts qui
doivent ou non déclencher une alerte, vérification de signature, dédoublonnage,
neutralisation du HTML hostile dans un nom de client, totaux du jour et de la
semaine, déclenchement des récapitulatifs (y compris après une coupure de plusieurs
jours).

## Ce qui se passe si quelque chose tombe

| Panne | Conséquence |
|---|---|
| Telegram momentanément injoignable | la pipeline répond 503, Stripe rejoue l'évènement (pendant 3 jours). L'alerte finit par arriver. |
| Jeton révoqué ou bot retiré du groupe | l'alerte est perdue, et le log dit `ALERTE PERDUE` en clair. |
| Variable d'environnement oubliée | 503 également : rien n'est perdu, `/health` affiche `degraded`. |
| La pipeline est arrêtée | Stripe rejoue tout à son retour : rien n'est perdu. |
| Requête non signée par Stripe | rejetée (400). Personne ne peut poster de fausse alerte. |
| Stripe envoie deux fois le même évènement | une seule alerte, et une seule souscription comptée. |
| Le service redémarre en cours de journée | les récapitulatifs déjà envoyés ne repartent pas ; ceux en retard sont rattrapés. |

## Organisation du code

| Fichier | Rôle |
|---|---|
| `app/serveur.py` | reçoit l'appel de Stripe, vérifie la signature, décide de la réponse HTTP |
| `app/alertes.py` | décide ce qui mérite une alerte, et met les messages en forme |
| `app/telegram.py` | poste sur Telegram, distingue panne passagère et erreur définitive |
| `app/recap.py` | déclenche les récapitulatifs aux heures voulues |
| `app/journal.py` | mémoire des évènements traités et des souscriptions comptées |
| `app/config.py` | toutes les variables d'environnement, en un seul endroit |
| `outils/trouver_chat_id.py` | retrouve l'identifiant du groupe Telegram |
| `outils/tester_telegram.py` | envoie une fausse alerte pour vérifier le groupe |
