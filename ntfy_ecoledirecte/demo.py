"""École simulée pour essayer le service sans compte EcoleDirecte.

Active avec la variable d'environnement NTFY_ED_DEMO=true, puis ajoute un compte dont
l'identifiant est « demo » (ou « demo-question » pour tester la question de sécurité).
Aucune connexion à EcoleDirecte n'est faite. Les événements arrivent minute par minute :

  +1 min  changement de salle        +4 min  contrôle annoncé
  +2 min  prof absent (cours annulé)  +5 min  nouveau message
  +3 min  nouvelle note               +6 min  retard en vie scolaire
et un cours commence quelques minutes après l'ajout du compte, pour voir le rappel.
"""

from __future__ import annotations

import datetime as dt
import os
import time

from .ecoledirecte import ErreurEcoleDirecte, QuestionSecuriteRequise

ACTIF = os.environ.get("NTFY_ED_DEMO", "").lower() in ("1", "true", "oui", "yes")
# Durée d'une « étape » de la démo, en secondes (60 par défaut ; plus court pour les tests).
PAS = max(5, int(os.environ.get("NTFY_ED_DEMO_PAS", "60") or 60))
_DEBUTS: dict[str, float] = {}  # identifiant -> moment de la première connexion


def est_demo(identifiant: str) -> bool:
    return ACTIF and identifiant.strip().lower() in ("demo", "demo-question")


def _maintenant():
    from .surveillance import maintenant
    return maintenant()


class SessionDemo:
    """Même interface que ecoledirecte.Session, avec des données inventées."""

    def __init__(self, identifiant, motdepasse="", fa=None, eleve_choisi=None):
        self.identifiant = identifiant.strip().lower()
        self.fa = fa
        self.token = None
        self.eleve = "1"
        self.eleves = [{"id": "1", "prenom": "Élève démo"}]
        self._t0 = _DEBUTS.setdefault(self.identifiant, time.time())
        self._ancrage = _ancre(self.identifiant)

    # ---- connexion et question de sécurité
    def connexion(self):
        if self.identifiant == "demo-question" and not self.fa:
            raise QuestionSecuriteRequise("EcoleDirecte demande de répondre à la question de sécurité.")
        self.token = "demo"

    def question_securite(self):
        if self.identifiant != "demo-question":
            raise ErreurEcoleDirecte("EcoleDirecte n'a pas demandé de question de sécurité : le compte fonctionne déjà.")
        return "Quel est votre mois de naissance ? (démo : réponds « Mars »)", ["Janvier", "Mars", "Septembre", "Décembre"]

    def repondre(self, index):
        if index != 1:
            raise ErreurEcoleDirecte("Mauvaise réponse ou session expirée. Recommence.")
        self.fa = {"cn": "demo", "cv": "demo"}
        self.token = "demo"
        return self.fa

    # ---- données
    def _etape(self):
        return int((time.time() - self._t0) // PAS)

    def emploi_du_temps(self, debut, fin):
        e = self._etape()
        base = self._ancrage
        def c(i, matiere, salle, prof, minutes, duree=55, annule=False):
            d = base + dt.timedelta(minutes=minutes)
            return str(i), {"matiere": matiere, "salle": salle, "prof": prof,
                            "debut": f"{d:%Y-%m-%d %H:%M}", "fin": f"{d + dt.timedelta(minutes=duree):%Y-%m-%d %H:%M}",
                            "annule": annule, "modifie": False}
        cours = dict([
            c(1, "Mathématiques", "B12", "M. Martin", 6),                       # rappel « 5 min avant » ~1 min après l'ajout
            c(2, "Anglais", "C07" if e >= 1 else "A04", "Mme Leroy", 65),       # +1 min : changement de salle
            c(3, "SVT", "L2", "M. Petit", 125, annule=e >= 2),                  # +2 min : prof absent
            c(4, "Histoire-géographie", "D01", "Mme Moreau", 24 * 60),
            c(5, "EPS", "Gymnase", "M. Roux", 24 * 60 + 120, duree=115),
        ])
        return cours

    def notes(self):
        n = {"n1": {"matiere": "Français", "devoir": "Rédaction", "valeur": "14", "sur": "20", "coef": "1", "moyenne_classe": "11,8"}}
        if self._etape() >= 3:
            n["n2"] = {"matiere": "Physique-chimie", "devoir": "Contrôle chapitre 3", "valeur": "16,5", "sur": "20",
                       "coef": "2", "moyenne_classe": "12,4"}
        return n

    def devoirs(self):
        demain = (_maintenant().date() + dt.timedelta(days=1)).isoformat()
        apres = (_maintenant().date() + dt.timedelta(days=3)).isoformat()
        d = {"d1": {"matiere": "Anglais", "pour": demain, "controle": False}}
        if self._etape() >= 4:
            d["d2"] = {"matiere": "Histoire-géographie", "pour": apres, "controle": True}
        return d

    def consignes_du_jour(self, date_rendu):
        return {"d1": "Exercice 4 page 52.", "d2": "Réviser la Révolution française, chapitres 2 et 3."}

    def messages(self, annee):
        m = {"m1": {"sujet": "Réunion parents-professeurs", "de": "Vie scolaire"}}
        if self._etape() >= 5:
            m["m2"] = {"sujet": "Sortie au musée jeudi", "de": "Mme Leroy"}
        return m

    def vie_scolaire(self):
        if self._etape() >= 6:
            return {"r1": {"type": "Retard", "libelle": "Retard", "date": f"{_maintenant():%d/%m}", "motif": "Transport"}}
        return {}


_ANCRES: dict[str, dt.datetime] = {}


def _ancre(identifiant):
    """Heure de référence des cours simulés : fixée à la première connexion."""
    if identifiant not in _ANCRES:
        _ANCRES[identifiant] = _maintenant().replace(second=0, microsecond=0)
    return _ANCRES[identifiant]
