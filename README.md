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

## Les messages

**Tout chiffre important est en première ligne** : Telegram n'affiche que le début
du message dans sa notification, l'essentiel doit donc être lisible sans ouvrir la
conversation.

À chaque souscription réelle :

```
🎉 3e souscription du jour · 929,76 € + 559,87 $ souscrits
Camille Martin — camille.martin@exemple.fr
ID by Rivoli — Standard · 19,00 € / mois
Total : 19,00 € / mois
Démarré le 11/09/2026
Voir dans Stripe
```

Cinq points dans la journée, plus un bilan la nuit — tout à l'heure de Paris. Deux
lignes : la journée (souscriptions et volume net), puis l'état du parc (MRR et
abonnés actifs).

```
📊 20h · 37 nouvelles souscriptions · 3 325,13 € net
MRR ≈ 128 706,92 € · 3738 abonnés actifs
```

```
🌙 Bilan du vendredi 11 septembre · 37 nouvelles souscriptions · 3 325,13 € net
MRR ≈ 128 706,92 € · 3738 abonnés actifs

7 derniers jours : 269 nouvelles souscriptions · 20 154,00 € net
```

Le **≈** devant le MRR est volontaire : il est recalculé ici à partir des
abonnements, pas lu chez Stripe, et s'écarte de quelques pourcents du tableau de
bord (taux de change fixe `TAUX_EUR_USD`, règles internes de Stripe non
documentées). Le chiffre exact n'est servi que par l'Analytics API de Stripe, en
préversion fermée — le jour où elle s'ouvre sur ce compte, le ≈ disparaît.

### Trois chiffres qui ne mesurent pas la même chose

| | Ce que c'est | D'où ça vient |
|---|---|---|
| **montant souscrit** (alertes) | le prix catalogue des nouvelles souscriptions du jour | les évènements d'abonnement |
| **volume net** (récapitulatifs) | l'argent réellement entré, renouvellements compris, moins les remboursements | les mouvements du solde Stripe |
| **MRR** (récapitulatifs) | le revenu mensuel récurrent de tout le parc d'abonnés | la liste des abonnements |

Un abonnement démarré en essai gratuit compte dans le premier et pas dans le
second : rien n'a encore été prélevé. Le libellé ne dit donc jamais « encaissé »
pour le montant souscrit.

Le volume net exclut deux choses qui ressemblent à des sorties d'argent sans en
être : les **virements vers votre banque** (l'argent change de poche) et les **frais
Stripe** (ils se déduisent du net bancaire, pas du volume des ventes). Les compter
faisait tomber une journée à 3 325 € encaissés à 487 € — un chiffre faux de 85 %.

### Règles de comptage

- **Chaque devise reste séparée** pour le montant souscrit : 19 € et 19 $ ne font
  pas 38. Le volume net, lui, est déjà converti par Stripe dans la devise du compte.
- **Les périodicités sont additionnées** au sein d'une devise : un annuel à 190 € et
  un mensuel à 19 € font 209 € souscrits ce jour-là.
- **Les abonnements du mode test ne comptent pas.** Vos essais ne polluent jamais
  les chiffres réels.
- **Un abonnement sans montant fixe** (tarification à paliers ou à l'usage) est
  compté mais pas chiffré ; le message le signale.

Une souscription est datée du **jour de l'évènement Stripe**, pas du moment où elle
est traitée : après une panne, les rattrapages retombent dans la bonne journée.

**Au démarrage, les compteurs se remplissent tout seuls** en relisant les derniers
jours d'évènements Stripe (`app/rattrapage.py`). Sans ça, le premier jour de service
annoncerait « 1re souscription du jour » sur une journée qui en compte déjà
trente-sept. Ce rattrapage n'envoie aucune alerte et ne compte jamais deux fois la
même souscription. Réglable via `RATTRAPAGE_JOURS` (0 pour le désactiver).

Le service peut redémarrer sans rien perdre. S'il était arrêté à 16h, le
récapitulatif manqué part au retour ; si plusieurs points du jour sont en retard, un
seul est envoyé. Les bilans de minuit partent tous — jusqu'à 3 jours en arrière —
car chacun est la seule trace chiffrée de sa journée.

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
« Powering an integration you built ». Trois permissions, toutes en **Read** :

| Permission | À quoi elle sert |
|---|---|
| **Customers** → Read | écrire « Marie Dupont — marie@exemple.fr » plutôt que « cus_123 » |
| **Products** → Read | écrire « ID by Rivoli — Standard » plutôt que « rivoli_standard » |
| **Events** → Read | relire l'historique au démarrage, pour que les compteurs ne partent pas de zéro |
| **Balance transactions** → Read | le volume net réellement encaissé |
| **Subscriptions** → Read | le MRR et le nombre d'abonnés actifs |

Chaque permission manquante retire une ligne des messages sans rien casser, et
`/health` dit laquelle : `rattrapage` (Events), `volume_net` (Balance transactions),
`mrr_et_abonnes` (Subscriptions). Cette pipeline n'écrit jamais rien dans Stripe : lui donner
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
{"status": "ok", "configuration_manquante": [], "enrichissement_stripe": true,
 "rattrapage": {"execute": true, "souscriptions": 274, "probleme": null}}
```

Un `probleme` non nul dans `rattrapage` signale que les compteurs sont partis de
zéro — le plus souvent la permission **Events (Read)** manquante sur la clé Stripe.

Si `status` vaut `degraded`, la liste `configuration_manquante` nomme la variable à
corriger. La réponse reste volontairement un code 200 : un 503 ferait échouer le
healthcheck de Railway, qui tuerait le service — et Stripe tomberait alors sur une
adresse morte au lieu du 503 qui déclenche le rejeu.

## Lancer les tests

```bash
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python tests/test_alertes.py
```

185 vérifications, sans aucun appel réseau : mise en forme des montants, statuts qui
doivent ou non déclencher une alerte, vérification de signature, dédoublonnage,
neutralisation du HTML hostile dans un nom de client, totaux du jour et de la
semaine, déclenchement des récapitulatifs (y compris après une coupure de plusieurs jours),
et rattrapage de l'historique.

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
| `app/rattrapage.py` | remplit les compteurs depuis l'historique Stripe au démarrage |
| `app/volume.py` | lit le volume net encaissé dans les mouvements du solde Stripe |
| `app/abonnes.py` | recalcule le MRR et compte les abonnés actifs |
| `app/stripe_client.py` | configure les deux clients Stripe (webhook rapide, tâches de fond patientes) |
| `app/journal.py` | mémoire des évènements traités et des souscriptions comptées |
| `app/config.py` | toutes les variables d'environnement, en un seul endroit |
| `outils/trouver_chat_id.py` | retrouve l'identifiant du groupe Telegram |
| `outils/tester_telegram.py` | envoie une fausse alerte pour vérifier le groupe |
