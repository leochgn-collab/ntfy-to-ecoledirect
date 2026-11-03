"""Surveillance de tous les comptes : détection des changements et rappels avant les cours."""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from zoneinfo import ZoneInfo

import requests

from . import notifications
from . import demo
from .ecoledirecte import ErreurEcoleDirecte, QuestionSecuriteRequise, Session, annee_scolaire

log = logging.getLogger(__name__)

FUSEAU = ZoneInfo(os.environ.get("TZ") or "Europe/Paris")
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def maintenant() -> dt.datetime:
    return dt.datetime.now(FUSEAU).replace(tzinfo=None)


def date_de(t: str) -> dt.datetime:
    return dt.datetime.strptime(t[:16], "%Y-%m-%d %H:%M")


def heure(d):
    return f"{d.hour}h{d.minute:02d}"


def jour(d):
    return f"{JOURS[d.weekday()]} {d.day}/{d.month:02d}"


def quand(d):
    return f"{jour(d)} à {heure(d)}"


def jour_de(t):
    try:
        return jour(dt.datetime.strptime(t[:10], "%Y-%m-%d"))
    except (ValueError, TypeError):
        return t or ""


def court(t, n=160):
    t = (t or "").strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


# ================================================================ fabrication des alertes

def details_cours(c):
    morceaux = [f"Salle {c['salle']}"] if c["salle"] else []
    if c["prof"]:
        morceaux.append(c["prof"])
    morceaux.append(f"{heure(date_de(c['debut']))}–{heure(date_de(c['fin']))}")
    return " · ".join(morceaux)


def changements_cours(ancien, nouveau, a_partir_de, ancienne_limite=None):
    """Compare deux emplois du temps {id: cours}. `ancienne_limite` : dernier jour de la fenêtre précédente,
    au-delà duquel un cours « apparu » vient seulement d'entrer dans la fenêtre surveillée."""
    alertes = []
    for cid, n in nouveau.items():
        if date_de(n["fin"]) < a_partir_de:
            continue
        a, d = ancien.get(cid), date_de(n["debut"])
        if a is None:
            if n["annule"] or (ancienne_limite and n["debut"][:10] >= ancienne_limite):
                continue
            alertes.append((f"Cours ajouté : {n['matiere']}", f"{quand(d)} · {details_cours(n)}", ["new"], 4))
            continue
        if n["annule"] and not a["annule"]:
            qui = f" ({n['prof']})" if n["prof"] else ""
            alertes.append((f"Cours annulé : {n['matiere']}", f"{quand(d)}{qui}. Prof absent ou cours supprimé.", ["x"], 4))
            continue
        if a["annule"] and not n["annule"]:
            alertes.append((f"Cours rétabli : {n['matiere']}", f"{quand(d)} · {details_cours(n)}", ["white_check_mark"], 4))
            continue
        if n["salle"] != a["salle"]:
            alertes.append((f"Changement de salle : {n['matiere']}",
                            f"{quand(d)} : salle {n['salle'] or '?'} au lieu de {a['salle'] or '?'}", ["door"], 4))
        if n["debut"] != a["debut"] or n["fin"] != a["fin"]:
            alertes.append((f"Changement d'horaire : {n['matiere']}",
                            f"Maintenant {quand(d)} (avant {quand(date_de(a['debut']))})", ["clock3"], 4))
        if n["prof"] != a["prof"] and n["prof"] and a["prof"]:
            alertes.append((f"Remplacement : {n['matiere']}", f"{quand(d)} avec {n['prof']} au lieu de {a['prof']}",
                            ["bust_in_silhouette"], 3))
    for cid, a in ancien.items():
        if cid not in nouveau and not a["annule"] and date_de(a["debut"]) > a_partir_de:
            alertes.append((f"Cours retiré : {a['matiere']}", f"{quand(date_de(a['debut']))} n'est plus à l'emploi du temps.",
                            ["x"], 4))
    return alertes


def alerte_note(n):
    note = n["valeur"] + (f"/{n['sur']}" if n["sur"] and n["valeur"] else "")
    morceaux = [n["devoir"]] if n["devoir"] else []
    if n["coef"] and n["coef"] not in ("1", "1.00", "1,00"):
        morceaux.append(f"coef {n['coef']}")
    if n["moyenne_classe"]:
        morceaux.append(f"moyenne de la classe {n['moyenne_classe']}")
    return f"Nouvelle note en {n['matiere']} : {note or 'voir EcoleDirecte'}", " · ".join(morceaux), ["pencil"], 4


def alerte_devoir(d):
    consigne = f" {court(d.get('consigne'))}" if d.get("consigne") else ""
    if d["controle"]:
        return f"Contrôle prévu en {d['matiere']}", f"Le {jour_de(d['pour'])}.{consigne}", ["warning"], 5
    return f"Nouveau devoir en {d['matiere']}", f"Pour le {jour_de(d['pour'])}.{consigne}", ["books"], 3


def alerte_message(m):
    return f"Message de {m['de']}", m["sujet"], ["email"], 4


def alerte_vie_scolaire(e):
    texte = " · ".join(x for x in ((e["libelle"] if e["libelle"] != e["type"] else ""), e["date"], e["motif"]) if x)
    return f"Vie scolaire : {e['type']}", texte, ["clipboard"], 4


def rappels_a_envoyer(cours, deja, minutes, instant):
    res = []
    for cid, c in cours.items():
        if c["annule"]:
            continue
        cle = f"{cid}@{c['debut']}"
        ecart = (date_de(c["debut"]) - instant).total_seconds()
        if 0 <= ecart <= minutes * 60 and cle not in deja:
            res.append((cle, c))
    return res


# ================================================================ un compte

class Surveillant:
    """Surveille un compte. `etat` est sauvegardé tel quel dans la base (aucun secret dedans)."""

    def __init__(self, compte: dict, reglages: dict, envoyer):
        self.id = compte["id"]
        self.nom = compte["nom"]
        self.version = compte["version"]
        self.config = compte["config"]
        self.reglages = reglages
        self.etat = compte["etat"]
        for cle, defaut in (("cours", {}), ("vus", {}), ("rappels", []), ("consignes", {}), ("controles", [])):
            self.etat.setdefault(cle, defaut)
        self.envoyer = envoyer          # envoyer(topic, titre, message, tags, priorite)
        sec = compte.get("secrets") or {}
        fabrique = demo.SessionDemo if demo.est_demo(compte["identifiant"]) else Session
        self.session = fabrique(compte["identifiant"], sec.get("motdepasse", ""), fa=sec.get("fa"),
                                eleve_choisi=self.config.get("eleve_id") or None)
        self.prochaine = 0.0

    def _changement(self, alerte):
        topic = self.config.get("topic_changements")
        if topic:
            self.envoyer(topic, *alerte)

    def verifier(self, notifier=True) -> list[str]:
        """Une vérification complète. Lève QuestionSecuriteRequise / ErreurEcoleDirecte si la connexion échoue."""
        cats = self.config["categories"]
        instant = maintenant()
        erreurs = []
        if not self.session.token:
            self.session.connexion()  # erreurs de connexion : remontées à l'appelant
        self.etat["eleves"] = self.session.eleves

        besoin_edt = cats.get("emploi_du_temps") or self.config.get("topic_rappels")
        if besoin_edt:
            try:
                debut = instant.date()
                fin = debut + dt.timedelta(days=int(self.reglages.get("jours_surveilles", 14)))
                cours = self.session.emploi_du_temps(f"{debut:%Y-%m-%d}", f"{fin:%Y-%m-%d}")
                if self.etat["cours"] and notifier and cats.get("emploi_du_temps"):
                    for a in changements_cours(self.etat["cours"], cours, instant, self.etat.get("limite")):
                        self._changement(a)
                self.etat["cours"] = cours
                self.etat["limite"] = f"{fin:%Y-%m-%d}"
            except (ErreurEcoleDirecte, requests.RequestException, ValueError, KeyError, TypeError) as e:
                erreurs.append(f"emploi du temps ({e})")

        sources = [
            ("notes", self.session.notes, alerte_note),
            ("devoirs", self.session.devoirs, alerte_devoir),
            ("messages", lambda: self.session.messages(annee_scolaire(instant.year, instant.month)), alerte_message),
            ("vie_scolaire", self.session.vie_scolaire, alerte_vie_scolaire),
        ]
        for nom, lire, fabriquer in sources:
            if not cats.get(nom):
                continue
            try:
                elements = lire()
                if nom == "devoirs":
                    self._consignes(elements)
                deja = self.etat["vus"].get(nom)
                if deja is not None and notifier:
                    deja_set = set(deja)
                    for cle, x in elements.items():
                        if cle not in deja_set:
                            self._changement(fabriquer(x))
                    if nom == "devoirs":
                        for cle in {k for k, d in elements.items() if d["controle"]} - set(self.etat["controles"]):
                            if cle in deja_set:  # devoir déjà connu devenu contrôle
                                self._changement(alerte_devoir(elements[cle]))
                if nom == "devoirs":
                    self.etat["controles"] = sorted(k for k, d in elements.items() if d["controle"])
                self.etat["vus"][nom] = sorted(set(deja or []) | set(elements))[-2000:]
            except (ErreurEcoleDirecte, requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
                erreurs.append(f"{nom.replace('_', ' ')} ({e})")

        self.etat["derniere_verif"] = instant.isoformat(timespec="minutes")
        self.etat["erreurs"] = erreurs
        return erreurs

    def _consignes(self, devoirs):
        connues = self.etat["consignes"]
        for date_rendu in sorted({d["pour"] for k, d in devoirs.items() if k not in connues})[:15]:
            connues.update(self.session.consignes_du_jour(date_rendu))
            for k, d in devoirs.items():
                if d["pour"] == date_rendu:
                    connues.setdefault(k, "")
        for k, d in devoirs.items():
            d["consigne"] = connues.get(k, "")
        self.etat["consignes"] = {k: v for k, v in connues.items() if k in devoirs}

    def rappels(self):
        topic = self.config.get("topic_rappels")
        if not topic:
            return False
        minutes = int(self.config.get("minutes_avant") or 5)
        envoyes = False
        for cle, c in rappels_a_envoyer(self.etat["cours"], set(self.etat["rappels"]), minutes, maintenant()):
            titre = f"Dans {minutes} min : {c['matiere']}" + (" (modifié)" if c["modifie"] else "")
            self.envoyer(topic, titre, details_cours(c), ["school"], 4)
            self.etat["rappels"].append(cle)
            envoyes = True
        self.etat["rappels"] = self.etat["rappels"][-300:]
        return envoyes


# ================================================================ tous les comptes

class Moteur:
    """Fil d'arrière-plan qui surveille tous les comptes actifs."""

    def __init__(self, stockage):
        self.stockage = stockage
        self.surveillants: dict[int, Surveillant] = {}
        self.reveil = threading.Event()
        self.arret = threading.Event()
        self.verrou = threading.Lock()

    def demarrer(self):
        threading.Thread(target=self._boucle, name="surveillance", daemon=True).start()

    def reveiller(self, ident: int | None = None):
        if ident in self.surveillants:
            self.surveillants[ident].prochaine = 0
        self.reveil.set()

    def _envoyeur(self, nom_compte):
        def envoyer(topic, titre, message, tags=None, priorite=3):
            try:
                notifications.envoyer(self.stockage.reglages_ntfy(), topic, titre, message, tags, priorite)
                log.info("[%s] notification « %s » envoyée", nom_compte, titre)
            except notifications.ErreurNtfy as e:
                log.warning("[%s] notification non envoyée : %s", nom_compte, e)
        return envoyer

    def _synchroniser(self):
        """Crée, met à jour ou retire les surveillants selon la base."""
        reglages = {"jours_surveilles": self.stockage.reglage("jours_surveilles")}
        actifs = {c["id"]: c for c in self.stockage.comptes(avec_secrets=True) if c["actif"]}
        for ident in list(self.surveillants):
            if ident not in actifs or actifs[ident]["version"] != self.surveillants[ident].version:
                del self.surveillants[ident]
        for ident, c in actifs.items():
            if ident not in self.surveillants and c["statut"] != "question":
                self.surveillants[ident] = Surveillant(c, reglages, self._envoyeur(c["nom"]))
        return reglages

    def _boucle(self):
        while not self.arret.is_set():
            try:
                with self.verrou:
                    self._tour()
            except Exception:  # la boucle ne doit jamais s'arrêter
                log.exception("erreur inattendue dans la surveillance")
            self.reveil.wait(min(30, demo.PAS) if demo.ACTIF else 30)
            self.reveil.clear()

    def _tour(self):
        self._synchroniser()
        intervalle = max(5, int(self.stockage.reglage("verif_minutes") or 10)) * 60
        if demo.ACTIF:
            intervalle = demo.PAS  # démo : une vérification à chaque étape
        for s in list(self.surveillants.values()):
            if time.time() >= s.prochaine:
                self._verifier(s, intervalle)
            if s.rappels():
                self.stockage.enregistrer_etat(s.id, s.etat)

    def _verifier(self, s: Surveillant, intervalle: int):
        premiere = not s.etat.get("derniere_verif")
        try:
            erreurs = s.verifier(notifier=not premiere)
            statut, message = ("ok", "") if not erreurs else ("partiel", "Illisible : " + "; ".join(erreurs))
            s.prochaine = time.time() + intervalle
        except QuestionSecuriteRequise:
            statut, message = "question", "Réponds à la question de sécurité dans l'interface."
            self.surveillants.pop(s.id, None)
            s._changement(("EcoleDirecte : question de sécurité", f"Compte « {s.nom} » en pause : ouvre l'interface pour répondre.",
                           ["warning"], 4))
        except ErreurEcoleDirecte as e:
            statut, message = "erreur", str(e)
            s.session.token = None
            s.prochaine = time.time() + 30 * 60
        except requests.RequestException as e:
            statut, message = "erreur", f"EcoleDirecte injoignable ({e.__class__.__name__}), nouvel essai dans 2 min."
            s.session.token = None
            s.prochaine = time.time() + 120
        self.stockage.enregistrer_etat(s.id, s.etat, statut, message)
