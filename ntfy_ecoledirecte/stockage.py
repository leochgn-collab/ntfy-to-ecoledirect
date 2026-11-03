"""Stockage SQLite : réglages, comptes EcoleDirecte (secrets chiffrés) et état de surveillance."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .securite import Coffre

CATEGORIES = {
    "emploi_du_temps": "Emploi du temps (annulations, salles, horaires, remplacements)",
    "notes": "Nouvelles notes",
    "devoirs": "Devoirs et contrôles",
    "messages": "Messages",
    "vie_scolaire": "Vie scolaire (absences, retards, sanctions)",
}

CONFIG_DEFAUT = {
    "topic_changements": "",
    "topic_rappels": "",
    "minutes_avant": 5,
    "categories": {c: True for c in CATEGORIES},
    "eleve_id": "",
}

REGLAGES_DEFAUT = {
    "ntfy_serveur": "https://ntfy.sh",
    "verif_minutes": 10,
    "jours_surveilles": 14,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS reglages (cle TEXT PRIMARY KEY, valeur TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comptes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    identifiant TEXT NOT NULL,
    secrets TEXT NOT NULL,
    config TEXT NOT NULL,
    etat TEXT NOT NULL DEFAULT '{}',
    actif INTEGER NOT NULL DEFAULT 1,
    statut TEXT NOT NULL DEFAULT 'nouveau',
    message TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1
);
"""


class Stockage:
    def __init__(self, dossier: Path):
        self.dossier = dossier
        self.coffre = Coffre(dossier)
        self.fichier = dossier / "ntfy-ecoledirecte.db"
        self._verrou = threading.RLock()
        with self._cnx() as c:
            c.executescript(SCHEMA)
        try:
            self.fichier.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _cnx(self):
        with self._verrou:
            c = sqlite3.connect(self.fichier, timeout=15)
            c.row_factory = sqlite3.Row
            try:
                c.execute("PRAGMA journal_mode=WAL")
                yield c
                c.commit()
            finally:
                c.close()

    # ------------------------------------------------------------ réglages

    def reglage(self, cle, defaut=None):
        with self._cnx() as c:
            r = c.execute("SELECT valeur FROM reglages WHERE cle=?", (cle,)).fetchone()
        if r is None:
            return REGLAGES_DEFAUT.get(cle, defaut)
        return json.loads(r["valeur"])

    def definir(self, cle, valeur):
        with self._cnx() as c:
            c.execute("INSERT INTO reglages(cle, valeur) VALUES(?, ?) "
                      "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur", (cle, json.dumps(valeur)))

    def secret(self, cle):
        return self.coffre.dechiffrer(self.reglage(cle))

    def definir_secret(self, cle, valeur):
        self.definir(cle, self.coffre.chiffrer(valeur) if valeur else None)

    def reglages_ntfy(self) -> dict:
        return {
            "serveur": self.reglage("ntfy_serveur"),
            "jeton": self.secret("ntfy_jeton") or "",
            "utilisateur": self.reglage("ntfy_utilisateur", "") or "",
            "motdepasse": self.secret("ntfy_motdepasse") or "",
        }

    # ------------------------------------------------------------ comptes

    def _vers_dict(self, r, avec_secrets=False) -> dict:
        d = {
            "id": r["id"], "nom": r["nom"], "identifiant": r["identifiant"],
            "config": {**CONFIG_DEFAUT, **json.loads(r["config"])},
            "etat": json.loads(r["etat"]), "actif": bool(r["actif"]),
            "statut": r["statut"], "message": r["message"], "version": r["version"],
        }
        d["config"]["categories"] = {**CONFIG_DEFAUT["categories"], **d["config"].get("categories", {})}
        if avec_secrets:
            d["secrets"] = self.coffre.dechiffrer(r["secrets"], {})
        return d

    def comptes(self, avec_secrets=False) -> list[dict]:
        with self._cnx() as c:
            lignes = c.execute("SELECT * FROM comptes ORDER BY nom COLLATE NOCASE, id").fetchall()
        return [self._vers_dict(r, avec_secrets) for r in lignes]

    def compte(self, ident: int, avec_secrets=False) -> dict | None:
        with self._cnx() as c:
            r = c.execute("SELECT * FROM comptes WHERE id=?", (ident,)).fetchone()
        return self._vers_dict(r, avec_secrets) if r else None

    def creer(self, nom, identifiant, motdepasse, config, actif=True) -> int:
        with self._cnx() as c:
            cur = c.execute("INSERT INTO comptes(nom, identifiant, secrets, config, actif) VALUES(?,?,?,?,?)",
                            (nom, identifiant, self.coffre.chiffrer({"motdepasse": motdepasse}),
                             json.dumps(config), int(actif)))
            return cur.lastrowid

    def modifier(self, ident, nom, identifiant, motdepasse, config, actif):
        """`motdepasse` vide = inchangé. Changer d'identifiant oublie la question de sécurité."""
        with self._cnx() as c:
            r = c.execute("SELECT * FROM comptes WHERE id=?", (ident,)).fetchone()
            if not r:
                return
            sec = self.coffre.dechiffrer(r["secrets"], {})
            if motdepasse:
                sec["motdepasse"] = motdepasse
            if identifiant != r["identifiant"]:
                sec.pop("fa", None)
            c.execute("UPDATE comptes SET nom=?, identifiant=?, secrets=?, config=?, actif=?, version=version+1, "
                      "statut=CASE WHEN ?=1 THEN 'nouveau' ELSE 'pause' END, message='' WHERE id=?",
                      (nom, identifiant, self.coffre.chiffrer(sec), json.dumps(config), int(actif), int(actif), ident))

    def supprimer(self, ident):
        with self._cnx() as c:
            c.execute("DELETE FROM comptes WHERE id=?", (ident,))

    def enregistrer_fa(self, ident, fa):
        with self._cnx() as c:
            r = c.execute("SELECT secrets FROM comptes WHERE id=?", (ident,)).fetchone()
            if not r:
                return
            sec = self.coffre.dechiffrer(r["secrets"], {})
            sec["fa"] = fa
            c.execute("UPDATE comptes SET secrets=?, statut='ok', message='', version=version+1 WHERE id=?",
                      (self.coffre.chiffrer(sec), ident))

    def enregistrer_etat(self, ident, etat, statut=None, message=None):
        with self._cnx() as c:
            if statut is None:
                c.execute("UPDATE comptes SET etat=? WHERE id=?", (json.dumps(etat, ensure_ascii=False), ident))
            else:
                c.execute("UPDATE comptes SET etat=?, statut=?, message=? WHERE id=?",
                          (json.dumps(etat, ensure_ascii=False), statut, message or "", ident))

    def definir_statut(self, ident, statut, message=""):
        with self._cnx() as c:
            c.execute("UPDATE comptes SET statut=?, message=? WHERE id=?", (statut, message, ident))
