"""Mémoire de la pipeline : ce qui a déjà été traité, et les souscriptions comptées.

Trois tables, trois rôles :

  evenements_traites  anti-doublon. Stripe garantit de livrer chaque évènement
                      « au moins une fois », pas « exactement une fois » : un même
                      evt_xxx peut arriver en double (rejeu après un timeout, ou
                      relance manuelle depuis le dashboard).

  souscriptions       une ligne par abonnement annoncé, avec son montant. C'est la
                      source des totaux du jour et de la semaine.

  recaps_envoyes      quels récapitulatifs horaires sont déjà partis. Sans ça, un
                      redémarrage du service renverrait ceux de la journée.

Une ligne de `evenements_traites` est écrite APRÈS l'envoi Telegram réussi, jamais
avant. C'est délibéré : si Telegram tombe, l'évènement reste « non traité », Stripe le
rejouera plus tard et l'alerte finira par passer. L'inverse perdrait l'alerte
définitivement. Le prix de ce choix est qu'un rejeu Stripe arrivant pendant qu'on
parle encore à Telegram peut produire un doublon — rare, et bien moins coûteux qu'une
alerte perdue.

`souscriptions`, elle, est écrite AVANT l'envoi : l'alerte annonce le total du jour,
elle doit donc déjà se compter elle-même. La clé primaire étant l'event_id, un rejeu
Stripe ne compte jamais deux fois la même souscription.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS evenements_traites (
    event_id   TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    traite_le  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS souscriptions (
    event_id        TEXT PRIMARY KEY,
    subscription_id TEXT,
    client          TEXT,
    montant         INTEGER,          -- part FIXE en centimes ; NULL si rien de fixe
    partiel         INTEGER NOT NULL DEFAULT 0,  -- 1 = une part est facturée à l'usage
    devise          TEXT,
    periodicite     TEXT,             -- '/ mois', '/ an'... jamais additionné entre elles
    jour            TEXT NOT NULL,    -- date à Paris, 'AAAA-MM-JJ' : les journées
                                      -- se découpent à minuit heure française
    horodatage      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_souscriptions_jour ON souscriptions(jour);

CREATE TABLE IF NOT EXISTS recaps_envoyes (
    creneau    TEXT PRIMARY KEY,      -- 'AAAA-MM-JJ H', ex. '2026-09-11 16'
    envoye_le  TEXT NOT NULL
);
"""


def _connexion(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def initialiser(db_path: Path) -> None:
    conn = _connexion(db_path)
    try:
        conn.executescript(SCHEMA)
        colonnes = {r["name"] for r in conn.execute("PRAGMA table_info(souscriptions)")}
        if "partiel" not in colonnes:
            # Base créée avant l'ajout de la colonne : migration légère plutôt qu'un
            # vrai système de migrations, la table reste très simple.
            conn.execute("ALTER TABLE souscriptions ADD COLUMN partiel INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


# --- anti-doublon --------------------------------------------------------------

def deja_traite(db_path: Path, event_id: str) -> bool:
    conn = _connexion(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM evenements_traites WHERE event_id = ?", (event_id,)
        ).fetchone() is not None
    finally:
        conn.close()


def marquer_traite(db_path: Path, event_id: str, type_evenement: str) -> None:
    conn = _connexion(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO evenements_traites (event_id, type, traite_le) "
            "VALUES (?, ?, ?)",
            (event_id, type_evenement, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# --- souscriptions comptées ----------------------------------------------------

def enregistrer_souscription(
    db_path: Path, event_id: str, subscription_id: str | None, client: str | None,
    montant: int | None, devise: str | None, periodicite: str | None, jour: str,
    partiel: bool = False,
) -> None:
    """INSERT OR IGNORE : un rejeu Stripe du même évènement ne recompte pas."""
    conn = _connexion(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO souscriptions (event_id, subscription_id, client, "
            "montant, devise, periodicite, jour, partiel, horodatage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, subscription_id, client, montant, devise, periodicite, jour,
             1 if partiel else 0, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def totaux(db_path: Path, jour_debut: str, jour_fin: str) -> dict:
    """Compte et montants entre deux dates Paris incluses ('AAAA-MM-JJ').

    Les montants sont regroupés par (devise, périodicité) et JAMAIS additionnés
    entre groupes : 19 €/mois et 190 €/an ne font pas 209 € — ce sont deux natures
    de revenu différentes, les mélanger produirait un chiffre qui ne veut rien dire.

    Deux nuances pour qu'un total ne paraisse jamais complet alors qu'il ne l'est
    pas, sans pour autant perdre du revenu connu :
      - `non_chiffrables` : aucun montant fixe du tout (100 % à l'usage). Comptées
        dans `nombre`, absentes des montants.
      - `partiels` : une part fixe connue, plus une part à l'usage. La part fixe
        COMPTE dans les montants — c'est du revenu certain — et le message signale
        qu'il y a un complément non chiffré.
    """
    conn = _connexion(db_path)
    try:
        lignes = conn.execute(
            "SELECT montant, devise, periodicite, partiel FROM souscriptions "
            "WHERE jour BETWEEN ? AND ?",
            (jour_debut, jour_fin),
        ).fetchall()
    finally:
        conn.close()

    groupes: dict[tuple[str, str], int] = {}
    non_chiffrables = 0
    partiels = 0
    for ligne in lignes:
        if ligne["partiel"]:
            partiels += 1
        if ligne["montant"] is None:
            non_chiffrables += 1
            continue
        cle = (ligne["devise"] or "", ligne["periodicite"] or "")
        groupes[cle] = groupes.get(cle, 0) + ligne["montant"]

    return {"nombre": len(lignes), "groupes": groupes,
            "non_chiffrables": non_chiffrables, "partiels": partiels}


# --- récapitulatifs déjà envoyés ------------------------------------------------

def recap_deja_envoye(db_path: Path, creneau: str) -> bool:
    conn = _connexion(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM recaps_envoyes WHERE creneau = ?", (creneau,)
        ).fetchone() is not None
    finally:
        conn.close()


def marquer_recap_envoye(db_path: Path, creneau: str) -> None:
    conn = _connexion(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO recaps_envoyes (creneau, envoye_le) VALUES (?, ?)",
            (creneau, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def aucun_recap_enregistre(db_path: Path) -> bool:
    """Vrai au tout premier démarrage. Sert à neutraliser les créneaux déjà passés
    ce jour-là : sans ça, un premier déploiement à 21h enverrait d'un coup les
    récapitulatifs de 12h, 16h, 18h et 20h."""
    conn = _connexion(db_path)
    try:
        return conn.execute("SELECT 1 FROM recaps_envoyes LIMIT 1").fetchone() is None
    finally:
        conn.close()
