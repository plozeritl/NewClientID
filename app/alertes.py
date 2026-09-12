"""Mise en forme des souscriptions réussies en messages Telegram.

Seules les créations d'abonnement QUI ABOUTISSENT déclenchent une alerte : ni les
échecs de paiement, ni les annulations, ni les abonnements restés au statut
'incomplete'. Voir _abonnement_cree pour le détail du piège.

Ce module ne fait QUE lire Stripe (.retrieve) pour enrichir l'alerte. Aucune
fonction d'écriture (remboursement, annulation, modification) n'y est définie, et la
clé API fournie doit de toute façon être restreinte à la lecture.

L'enrichissement (nom du client, nom de la formule) est toujours facultatif :
la clé Stripe utilisée en production est une clé restreinte, dont les scopes ne
couvrent pas forcément Customer ou Product. Une alerte un peu moins jolie vaut
infiniment mieux qu'une alerte perdue, donc toute erreur d'enrichissement est
avalée et l'identifiant brut affiché à la place.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import config
from app.stripe_client import stripe

logger = logging.getLogger("alertes.formatage")


# Devises sans sous-unité : leur montant n'est PAS en centimes, le diviser par 100
# afficherait 100x trop peu. Liste Stripe des "zero-decimal currencies".
DEVISES_SANS_CENTIMES = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
    "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}

SYMBOLES = {"eur": "€", "usd": "$", "gbp": "£"}

# (singulier, pluriel) : « / 2 an » se lisait faux.
INTERVALLES = {
    "day": ("jour", "jours"),
    "week": ("semaine", "semaines"),
    "month": ("mois", "mois"),
    "year": ("an", "ans"),
}

# Les deux seuls statuts où l'abonnement tourne vraiment. Tous les autres
# ('incomplete', 'past_due', 'unpaid', 'canceled'...) veulent dire que l'argent
# n'est pas rentré : pas d'alerte.
STATUTS_ACTIFS = {"active", "trialing"}


def _montant(valeur: int | None, devise: str | None) -> str | None:
    """'2900' + 'eur' -> '29,00 €'. None si le montant n'est pas chiffrable."""
    if valeur is None or not devise:
        return None
    devise = devise.lower()
    if devise in DEVISES_SANS_CENTIMES:
        texte = f"{valeur:,}".replace(",", " ")
    else:
        texte = f"{valeur / 100:,.2f}".replace(",", " ").replace(".", ",")
    symbole = SYMBOLES.get(devise)
    return f"{texte} {symbole}" if symbole else f"{texte} {devise.upper()}"


def _periodicite(price: dict) -> str | None:
    """'/ mois', '/ 3 mois', ou None si le prix n'est pas récurrent."""
    recurring = price.get("recurring") or {}
    formes = INTERVALLES.get(recurring.get("interval") or "")
    if not formes:
        return None
    nombre = recurring.get("interval_count") or 1
    return f"/ {formes[0]}" if nombre == 1 else f"/ {nombre} {formes[1]}"


# Stripe horodate en UTC. Un abonnement souscrit à 00h30 à Paris se produit à 22h30
# UTC la veille : affiché en UTC, il serait annoncé avec la date de la veille, juste
# à côté de l'heure du message Telegram. Repli sur UTC si la base de fuseaux manque.
try:
    FUSEAU = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover - dépend de l'image système
    FUSEAU = timezone.utc


def _date_fr(horodatage: int | None) -> str | None:
    if not horodatage:
        return None
    return datetime.fromtimestamp(horodatage, tz=FUSEAU).strftime("%d/%m/%Y")


def _lien_dashboard(livemode: bool, chemin: str) -> str:
    """Les objets du mode test vivent sous /test/ dans le dashboard Stripe : sans ce
    préfixe, un lien vers un abonnement de test tombe sur une page 'introuvable'."""
    prefixe = "" if livemode else "/test"
    return f"https://dashboard.stripe.com{prefixe}/{chemin}"


def _nom_client(customer) -> str:
    """'Marie Dupont — marie@exemple.fr', ou l'identifiant brut si Stripe ne répond pas."""
    if isinstance(customer, dict):
        client = customer
    elif isinstance(customer, str) and customer:
        if not stripe.api_key:
            return customer
        try:
            client = stripe.Customer.retrieve(customer).to_dict()
        except Exception as exc:
            logger.warning("Enrichissement client %s impossible : %s", customer, exc)
            return customer
    else:
        return "client inconnu"

    nom = (client.get("name") or "").strip()
    email = (client.get("email") or "").strip()
    if nom and email:
        return _echapper(f"{nom} — {email}")
    return _echapper(nom or email or client.get("id") or "client inconnu")


def _nom_formule(price: dict) -> str:
    """Le libellé lisible d'un prix : son surnom, sinon le nom du produit, sinon son id."""
    surnom = (price.get("nickname") or "").strip()
    if surnom:
        return _echapper(surnom)

    produit = price.get("product")
    if isinstance(produit, dict):
        return _echapper(
            (produit.get("name") or "").strip() or produit.get("id") or "formule inconnue"
        )
    # Pas de filtre sur un préfixe « prod_ » : Stripe laisse choisir librement
    # l'identifiant d'un produit, et les produits d'ID by Rivoli s'appellent
    # 'rivoli_standard', 'rivoli_pro'... Exiger le préfixe faisait afficher
    # « formule inconnue » sur les abonnements les plus courants — constaté le
    # 11/09/2026 en interrogeant le vrai compte.
    if isinstance(produit, str) and produit:
        if not stripe.api_key:
            return _echapper(produit)
        try:
            nom = stripe.Product.retrieve(produit).to_dict().get("name")
            return _echapper((nom or "").strip() or produit)
        except Exception as exc:
            logger.warning("Enrichissement produit %s impossible : %s", produit, exc)
            return _echapper(produit)
    return _echapper(price.get("id") or "formule inconnue")


def _analyser_abonnement(subscription: dict, libelles: bool = True) -> dict:
    """Décrit l'abonnement : une ligne par formule, et son montant périodique chiffré.

    `total_centimes` porte la part FIXE, celle qui est connue d'avance. `partiel`
    dit qu'au moins une ligne échappe au calcul. Les deux sont séparés exprès :
    un abonnement « 29 €/mois + dépassements à l'usage » a bien 29 € de revenu
    certain, et les faire disparaître des totaux du jour serait une perte sèche —
    alors qu'afficher « Total : 29 € » sur l'alerte elle-même serait mensonger.
    D'où : le montant compte dans les cumuls, mais aucun total n'est affiché sur
    l'alerte tant qu'une ligne manque.

    `libelles=False` saute la résolution des noms de formules, qui est la seule
    partie à interroger Stripe. Le rattrapage n'affiche aucun nom : sans ce
    raccourci, relire une semaine d'historique déclenchait un appel réseau par
    ligne d'abonnement — des centaines d'allers-retours en série au démarrage.

    Une ligne échappe au calcul dans trois cas :
      - tarification à paliers : unit_amount vaut None ;
      - tarification à l'usage (metered) : unit_amount est le prix UNITAIRE et la
        quantité n'est connue qu'en fin de période, donc aucun montant périodique
        n'existe encore ;
      - quantité absente du payload.
    """
    lignes: list[str] = []
    total = 0
    lignes_chiffrees = 0
    partiel = False
    devise = None
    periodicite = None

    for item in (subscription.get("items") or {}).get("data") or []:
        price = item.get("price") or {}
        quantite = item.get("quantity")
        devise = devise or price.get("currency")
        periodicite = periodicite or _periodicite(price)

        unitaire = price.get("unit_amount")
        a_lusage = (price.get("recurring") or {}).get("usage_type") == "metered"
        if a_lusage:
            partiel = True
            prix_unitaire = _montant(unitaire, price.get("currency"))
            montant = f"{prix_unitaire} par unité" if prix_unitaire else "facturé à l'usage"
        elif unitaire is None:
            partiel = True
            montant = "montant variable"
        elif quantite is None:
            partiel = True
            montant = _montant(unitaire, price.get("currency")) or "montant inconnu"
        else:
            total += unitaire * quantite
            lignes_chiffrees += 1
            montant = _montant(unitaire * quantite, price.get("currency")) or "montant inconnu"

        libelle = _nom_formule(price) if libelles else ""
        if quantite is not None and quantite != 1:
            libelle = f"{libelle} ×{quantite}"
        suffixe = f" {_periodicite(price)}" if _periodicite(price) else ""
        lignes.append(f"{libelle} · {montant}{suffixe}")

    montant_connu = lignes_chiffrees > 0 and _montant(total, devise) is not None
    complet = bool(lignes) and not partiel and montant_connu
    return {
        "lignes": lignes,
        "total_centimes": total if montant_connu else None,
        "partiel": partiel,
        "devise": devise,
        "periodicite": periodicite,
        "total_texte": (
            f"{_montant(total, devise)} {periodicite}".strip() if complet and periodicite
            else (_montant(total, devise) if complet else None)
        ),
    }


def analyser(contexte: dict) -> dict:
    """L'analyse de l'abonnement, calculée une seule fois et gardée dans le contexte.

    Elle résout les noms de formules, donc interroge Stripe. Le serveur en a besoin
    deux fois — pour compter la souscription, puis pour composer le message — et
    sans cette mise en cache chaque alerte interrogeait Stripe deux fois de suite,
    sur le chemin critique du webhook.
    """
    if "analyse" not in contexte:
        contexte["analyse"] = _analyser_abonnement(contexte["abonnement"])
    return contexte["analyse"]


def _chiffres(analyse: dict) -> tuple[int | None, str | None, str | None, bool]:
    return (analyse["total_centimes"], analyse["devise"], analyse["periodicite"],
            bool(analyse["partiel"]))


def resume_chiffre(contexte: dict) -> tuple[int | None, str | None, str | None, bool]:
    """(montant fixe en centimes, devise, périodicité, part à l'usage) — ce que le
    journal enregistre pour totaliser la journée. Réutilise l'analyse mise en cache,
    qui servira aussi à composer le message : un seul aller-retour Stripe."""
    return _chiffres(analyser(contexte))


def resume_chiffre_sans_reseau(subscription: dict) -> tuple[int | None, str | None, str | None, bool]:
    """La même chose, sans interroger Stripe. Pour le rattrapage, qui compte des
    souscriptions sans jamais afficher leur libellé."""
    return _chiffres(_analyser_abonnement(subscription, libelles=False))


def _echapper(texte: str) -> str:
    """Neutralise le HTML dans les valeurs venues de Stripe.

    Le nom et l'email sont saisis par le CLIENT lui-même au moment du paiement.
    Deux dangers, pas un :

      - « <a href="https://piege.example">Payer ici</a> » ferait un vrai lien
        cliquable dans le groupe. Telegram lit le HTML : échapper & < > le désamorce.
      - un retour à la ligne dans le nom fabriquerait une LIGNE ENTIÈRE du message :
        un client nommé « Marie\n*Total : 9 999,00 € / mois* » ferait apparaître un
        faux total en gras au-dessus du vrai. Les lignes sont assemblées avec des
        retours à la ligne : aucune valeur venue de Stripe n'a le droit d'en contenir.

    Appliqué à chaque VALEUR (nom, email, libellé de formule) et non aux lignes
    entières : nos propres balises <b> et <i>, elles, doivent rester du HTML.
    """
    sans_sauts = " ".join(texte.replace("\r", "\n").split("\n"))
    return sans_sauts.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bloc(titre: str, lignes: list[str], url: str | None = None,
          libelle_lien: str = "") -> str:
    """Le message complet, en HTML Telegram. Les lignes arrivent déjà échappées
    côté valeurs (voir _echapper) et peuvent porter nos propres <b> et <i>."""
    corps = "\n".join(lignes)
    message = f"<b>{titre}</b>\n{corps}" if corps else f"<b>{titre}</b>"
    if url:
        message += f'\n<a href="{url}">{libelle_lien}</a>'
    return message


# --- un formateur par évènement ------------------------------------------------

def _message_nouvel_abonnement(
    objet: dict, livemode: bool, analyse: dict, cumul: str | None = None,
    etat: str | None = None,
) -> str:
    lignes_formules, total = analyse["lignes"], analyse["total_texte"]
    lignes = [_nom_client(objet.get("customer"))]
    lignes += lignes_formules
    if total:
        # Le total est calculé sur les prix catalogue : un coupon (-50 %, premier mois
        # offert...) n'y est pas déduit. Le dire, plutôt que d'annoncer un chiffre
        # d'affaires faux.
        remise = objet.get("discounts") or objet.get("discount")
        libelle_total = "Total avant remise" if remise else "Total"
        lignes.append(f"<b>{libelle_total} : {total}</b>")
        if remise:
            lignes.append("<i>Une remise s'applique : le montant réellement payé est inférieur.</i>")

    essai = _date_fr(objet.get("trial_end"))
    if essai:
        lignes.append(f"<i>Période d'essai jusqu'au {essai}</i>")

    debut = _date_fr(objet.get("start_date") or objet.get("created"))
    if debut:
        lignes.append(f"Démarré le {debut}")

    # Le titre porte le compte du jour : c'est la seule ligne visible dans la
    # notification Telegram. En mode test, il annonce quand même un abonnement —
    # un titre « aucune souscription réelle aujourd'hui » sous une fusée serait
    # une alerte qui se contredit elle-même.
    if not livemode:
        titre = "🧪 Abonnement de test" + (f" · {etat}" if etat else "")
    elif cumul:
        titre = f"🎉 {cumul}"
    else:
        titre = "🎉 Nouvel abonnement"

    return _bloc(
        titre,
        lignes,
        _lien_dashboard(livemode, f"subscriptions/{objet.get('id')}"),
        "Voir dans Stripe",
    )


def _retenir_creation(objet: dict, precedent: dict) -> dict | None:
    """Stripe émet 'customer.subscription.created' AVANT de savoir si le premier
    paiement passe : un abonnement dont la carte est refusée naît quand même, au
    statut 'incomplete'. Alerter dessus annoncerait des clients qui n'ont rien payé.
    On ne retient donc qu'un abonnement réellement parti (actif, ou en essai) ; les
    'incomplete' sont rattrapés par _retenir_activation si leur paiement aboutit."""
    return objet if objet.get("status") in STATUTS_ACTIFS else None


def _retenir_activation(objet: dict, precedent: dict) -> dict | None:
    """Le rattrapage du cas ci-dessus : le client a corrigé son paiement (3-D Secure
    validé, autre carte...) et l'abonnement passe 'incomplete' -> actif. C'est à cet
    instant que la souscription a vraiment réussi.

    Uniquement depuis 'incomplete' : un retour depuis 'past_due' ou 'paused' est la
    reprise d'un abonnement existant, pas une nouvelle souscription — alerter dessus
    annoncerait un faux nouveau client."""
    if precedent.get("status") != "incomplete":
        return None
    return objet if objet.get("status") in STATUTS_ACTIFS else None


DECIDEURS = {
    "customer.subscription.created": _retenir_creation,
    "customer.subscription.updated": _retenir_activation,
}


def evaluer(evenement: dict) -> dict | None:
    """L'abonnement à annoncer, ou None si l'évènement ne mérite aucune alerte.

    Séparé de la mise en forme, et sans le moindre appel réseau : le serveur doit
    pouvoir décider s'il y a matière à alerter — et donc à compter la souscription
    dans les totaux du jour — avant d'aller chercher le nom du client chez Stripe.
    """
    decideur = DECIDEURS.get(evenement.get("type") or "")
    if not decideur:
        return None

    data = evenement.get("data") or {}
    retenu = decideur(data.get("object") or {}, data.get("previous_attributes") or {})
    if retenu is None:
        return None
    return {"abonnement": retenu, "livemode": bool(evenement.get("livemode"))}


def rendre(contexte: dict, cumul: str | None = None, etat: str | None = None) -> str:
    """Le message Telegram d'un abonnement retenu. C'est ici, et seulement ici, que
    l'on interroge Stripe pour remplacer les identifiants par des noms lisibles."""
    message = _message_nouvel_abonnement(
        contexte["abonnement"], contexte["livemode"], analyser(contexte), cumul, etat
    )
    # Pas de bandeau séparé au-dessus : il prenait la première ligne, la seule que
    # Telegram montre dans la notification, et repoussait les chiffres. Le mode
    # test est signalé dans le titre lui-même (« 🧪 Abonnement de test »).
    return message


def formater(evenement: dict, cumul: str | None = None,
             etat: str | None = None) -> str | None:
    """Les deux étapes d'un coup. Pratique pour les tests et les outils ; le serveur,
    lui, passe par evaluer() puis rendre() pour compter la souscription entre les deux."""
    contexte = evaluer(evenement)
    return rendre(contexte, cumul, etat) if contexte else None


# --- totaux et récapitulatifs --------------------------------------------------

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def libelle_jour(jour: datetime) -> str:
    """'vendredi 11 septembre' — sans dépendre de la locale du système, qui n'est
    pas garantie dans un conteneur Docker."""
    return f"{JOURS[jour.weekday()]} {jour.day} {MOIS[jour.month - 1]}"


def _formuler_groupes(groupes: dict) -> list[str]:
    """['859,78 €', '474,89 $'] — le montant SOUSCRIT, une somme par devise.

    Les périodicités sont additionnées entre elles (un annuel à 190 € et un mensuel
    à 19 € valent 209 € de souscriptions ce jour-là), mais jamais les devises :
    19 € et 19 $ ne font pas 38.

    Ce n'est PAS de l'argent encaissé, et le libellé ne doit jamais le laisser
    croire : un abonnement démarré en période d'essai ou avec un premier mois
    offert compte ici pour son prix catalogue alors que Stripe n'a rien prélevé.
    Le vrai volume net viendra d'une autre source.
    """
    formules = []
    for devise, total in sorted(groupes.items()):
        montant = _montant(total, devise)
        if montant:
            formules.append(montant)
    return formules


def _souscriptions_au_pluriel(nombre: int) -> str:
    return "1 nouvelle souscription" if nombre == 1 else f"{nombre} nouvelles souscriptions"


def phrase_cumul(totaux_jour: dict, volume: dict | None = None) -> str:
    """Le TITRE d'une alerte réelle : « 12e souscription du jour · 3 442,54 € net ».

    Placé en première ligne, et pas en bas : Telegram n'affiche que le début du
    message dans sa notification. L'essentiel — combien aujourd'hui, pour quel
    volume net — doit donc tenir là, sans avoir à ouvrir la conversation. Même
    contenu que la première ligne des récapitulatifs, pour qu'un coup d'œil suffise
    quel que soit le message.
    """
    nombre = totaux_jour.get("nombre") or 0
    ordinal = "1re" if nombre <= 1 else f"{nombre}e"
    encaisse = _volume_lisible(volume)
    if encaisse:
        return f"{ordinal} souscription du jour · {encaisse} net"
    return f"{ordinal} souscription du jour"


def phrase_etat_journee(totaux_jour: dict, volume: dict | None = None) -> str:
    """Le même titre pour une alerte du mode test, qui n'est jamais comptée : il
    annonce l'état réel de la journée sans s'y inclure."""
    nombre = totaux_jour.get("nombre") or 0
    encaisse = _volume_lisible(volume)
    if nombre == 0 and not encaisse:
        return "aucune souscription réelle aujourd'hui"
    detail = f" · {encaisse} net" if encaisse else ""
    return f"journée en cours : {_souscriptions_au_pluriel(nombre)}{detail}"


def _reserves(totaux: dict) -> list[str]:
    """Les réserves à afficher sous un total : ce qui n'a pas pu être chiffré. Vide
    la plupart du temps — et alors le message se réduit à son titre."""
    lignes = []
    non_chiffrables = totaux.get("non_chiffrables") or 0
    partiels = totaux.get("partiels") or 0
    if non_chiffrables:
        lignes.append(
            f"<i>dont {non_chiffrables} sans montant fixe (tarification à paliers "
            f"ou à l'usage)</i>"
        )
    complements = partiels - non_chiffrables
    if complements > 0:
        lignes.append(
            f"<i>dont {complements} avec une part variable, non chiffrée ici</i>"
        )
    return lignes


def _titre_chiffre(prefixe: str, totaux: dict, volume: dict | None = None) -> str:
    """« 📊 20h · 37 souscriptions · 3 325,13 € net ».

    Souscriptions du jour et volume net encaissé, dans le titre : c'est la seule
    ligne que Telegram affiche dans sa notification.
    """
    nombre = totaux.get("nombre") or 0
    morceaux = [prefixe]
    morceaux.append(_souscriptions_au_pluriel(nombre) if nombre else "aucune souscription")
    encaisse = _volume_lisible(volume)
    if encaisse:
        morceaux.append(f"{encaisse} net")
    return " · ".join(morceaux)


def _volume_lisible(volume: dict | None) -> str | None:
    """« 3 325,13 € » — None si Stripe n'a pas pu répondre. Distinguer « on ne sait
    pas » de « zéro » est essentiel : un récapitulatif qui annonce 0 € encaissé
    alors que la permission manque serait un mensonge."""
    if volume is None:
        return None
    montants = _formuler_groupes(volume)
    return " + ".join(montants) if montants else _montant(0, "eur")


def ligne_indicateurs(etat: dict | None) -> list[str]:
    """La deuxième ligne : les abonnés actifs. Vide tant que Stripe ne donne pas
    accès aux abonnements.

    Le MRR recalculé n'y figure plus : à +3,6 % du tableau de bord Stripe, il a été
    jugé trop faux pour être affiché (12/09/2026). Il reste calculé et visible dans
    /health, à titre indicatif, jusqu'à ce que l'Analytics API de Stripe — la seule
    à servir le chiffre exact — soit ouverte sur ce compte.
    """
    if not etat or etat.get("abonnes") is None:
        return []
    return [f"<b>{etat['abonnes']}</b> abonnés actifs"]


def message_point_du_jour(heure: int, totaux_jour: dict,
                          volume_net: dict | None = None,
                          indicateurs: dict | None = None) -> str:
    """Le récapitulatif intermédiaire (12h, 16h, 18h, 20h, 22h) : la journée en cours,
    depuis minuit.

    Tout tient dans le titre, pour être lisible dans la notification. Le corps ne
    porte que les réserves éventuelles (souscriptions non chiffrables) : le répéter
    en entier ferait un message qui se dit deux fois.
    """
    return _bloc(_titre_chiffre(f"📊 {heure}h", totaux_jour, volume_net),
                 ligne_indicateurs(indicateurs) + _reserves(totaux_jour))


def message_bilan(jour: datetime, totaux_jour: dict, totaux_semaine: dict,
                  volume_net: dict | None = None, indicateurs: dict | None = None,
                  volume_semaine: dict | None = None) -> str:
    """Le bilan de minuit : la journée qui vient de se terminer, plus les sept
    derniers jours glissants."""
    # Les réserves de la journée portent leur intitulé : sans lui, elles se collent
    # à la ligne des 7 jours et se lisent comme si elles la concernaient — les deux
    # blocs ayant exactement la même formulation.
    lignes = ligne_indicateurs(indicateurs)
    lignes += [f"<i>Journée : {r[3:]}" if r.startswith("<i>") else r
               for r in _reserves(totaux_jour)]
    if lignes:
        lignes.append("")

    semaine = [f"7 derniers jours : <b>{_souscriptions_au_pluriel(totaux_semaine.get('nombre') or 0)}</b>"]
    encaisse = _volume_lisible(volume_semaine)
    if encaisse:
        semaine[0] += f" · <b>{encaisse}</b> net"
    lignes += semaine + _reserves(totaux_semaine)

    return _bloc(_titre_chiffre(f"🌙 Bilan du {libelle_jour(jour)}", totaux_jour, volume_net),
                 lignes)
