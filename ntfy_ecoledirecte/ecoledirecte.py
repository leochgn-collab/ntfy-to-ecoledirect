"""Client minimal pour l'interface (non officielle) d'EcoleDirecte.

EcoleDirecte ne publie pas d'API : ce module reproduit les appels du site web.
Il ne journalise jamais d'identifiant, de mot de passe ni de jeton.
"""

from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass, field

import requests

API = "https://api.ecoledirecte.com/v3"
VERSION = "4.100.6"
# EcoleDirecte invalide la session si le navigateur annoncé change : on en garde un seul.
NAVIGATEUR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DELAI = 30  # secondes


class ErreurEcoleDirecte(Exception):
    """Erreur affichable telle quelle à l'utilisateur (sans donnée sensible)."""


class QuestionSecuriteRequise(ErreurEcoleDirecte):
    """EcoleDirecte demande de répondre à la question de sécurité (nouvel appareil)."""


def b64(texte: str | None) -> str:
    try:
        return base64.b64decode(texte or "").decode("utf-8", errors="replace")
    except ValueError:
        return ""


def texte_brut(html_b64: str | None, longueur: int = 600) -> str:
    """Contenu HTML encodé en base64 -> texte court."""
    t = re.sub(r"<[^>]+>", " ", b64(html_b64))
    t = re.sub(r"\s+", " ", html.unescape(t)).strip()
    return t if len(t) <= longueur else t[:longueur - 1].rstrip() + "…"


def joli(nom: str | None) -> str:
    nom = (nom or "").strip()
    # « MATHEMATIQUES » -> « Mathematiques » ; les sigles courts (SVT, EPS) restent tels quels.
    return nom.capitalize() if nom.isupper() and len(nom) > 4 else nom


def annee_scolaire(annee: int, mois: int) -> str:
    return f"{annee}-{annee + 1}" if mois >= 8 else f"{annee - 1}-{annee}"


@dataclass
class Session:
    """Une connexion à un compte EcoleDirecte."""

    identifiant: str
    motdepasse: str
    fa: dict | None = None               # preuve de la question de sécurité (cn/cv)
    eleve_choisi: str | None = None      # pour un compte parent avec plusieurs enfants
    token: str | None = None
    eleve: str | None = None
    boite: str | None = None
    eleves: list = field(default_factory=list)

    def __post_init__(self):
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": NAVIGATEUR,
                                  "Content-Type": "application/x-www-form-urlencoded"})
        self._question: list | None = None

    # ------------------------------------------------------------ bas niveau

    def _appel(self, chemin: str, donnees: dict | None = None, verbe: str = "get", params: dict | None = None) -> dict:
        p = {"verbe": verbe, "v": VERSION, **(params or {})}
        entetes = {"X-Token": self.token} if self.token else {}
        r = self.http.post(API + chemin, params=p, headers=entetes, timeout=DELAI,
                           data="data=" + json.dumps(donnees or {}, ensure_ascii=False))
        r.raise_for_status()
        try:
            rep = r.json()
        except ValueError as e:
            raise ErreurEcoleDirecte("réponse illisible d'EcoleDirecte") from e
        if rep.get("token"):
            self.token = rep["token"]
        return rep

    def _login(self, corps: dict) -> dict:
        # Jeton GTK à usage unique, exigé avant chaque connexion.
        self.http.get(API + "/login.awp", params={"gtk": 1, "v": VERSION}, timeout=DELAI)
        gtk = self.http.cookies.get("GTK")
        if gtk:
            self.http.headers["X-Gtk"] = gtk
        try:
            return self._appel("/login.awp", corps, verbe="post")
        finally:
            self.http.headers.pop("X-Gtk", None)

    # ------------------------------------------------------------ connexion

    def connexion(self) -> None:
        self.token = None
        corps = {"identifiant": self.identifiant, "motdepasse": self.motdepasse, "isRelogin": False, "uuid": ""}
        if self.fa:
            corps["fa"] = [self.fa]
        rep = self._login(corps)
        code = rep.get("code")
        if code == 250:
            raise QuestionSecuriteRequise("EcoleDirecte demande de répondre à la question de sécurité.")
        if code == 505:
            raise ErreurEcoleDirecte("Identifiant ou mot de passe EcoleDirecte incorrect.")
        if code != 200:
            raise ErreurEcoleDirecte(f"Connexion refusée par EcoleDirecte (code {code}).")
        compte = (rep.get("data") or {}).get("accounts", [{}])[0]
        if compte.get("typeCompte") == "E":
            self.eleve = str(compte["id"])
            self.boite = f"/eleves/{compte['id']}"
            self.eleves = [{"id": str(compte["id"]), "prenom": compte.get("prenom", "")}]
        else:
            self.eleves = [{"id": str(e["id"]), "prenom": e.get("prenom", "")}
                           for e in (compte.get("profile") or {}).get("eleves", [])]
            if not self.eleves:
                raise ErreurEcoleDirecte("Aucun élève trouvé sur ce compte parent.")
            ids = [e["id"] for e in self.eleves]
            self.eleve = self.eleve_choisi if self.eleve_choisi in ids else ids[0]
            self.boite = f"/familles/{compte['id']}"

    def question_securite(self) -> tuple[str, list[str]]:
        """Lance une connexion et renvoie (question, propositions) en clair."""
        self.fa = None
        try:
            self.connexion()
        except QuestionSecuriteRequise:
            pass
        else:
            raise ErreurEcoleDirecte("EcoleDirecte n'a pas demandé de question de sécurité : le compte fonctionne déjà.")
        rep = self._appel("/connexion/doubleauth.awp", {}, verbe="get")
        if rep.get("code") != 200:
            raise ErreurEcoleDirecte(f"Impossible d'obtenir la question de sécurité (code {rep.get('code')}).")
        self._question = rep["data"]["propositions"]
        return b64(rep["data"]["question"]), [b64(p) for p in self._question]

    def repondre(self, index: int) -> dict:
        """Envoie la réponse choisie. Renvoie le `fa` à conserver (chiffré) pour les prochaines connexions."""
        if not self._question or not 0 <= index < len(self._question):
            raise ErreurEcoleDirecte("Réponse invalide, recommence.")
        rep = self._appel("/connexion/doubleauth.awp", {"choix": self._question[index]}, verbe="post")
        if rep.get("code") != 200:
            raise ErreurEcoleDirecte("Mauvaise réponse ou session expirée. Recommence.")
        self.fa = {"cn": rep["data"]["cn"], "cv": rep["data"]["cv"]}
        self._question = None
        self.connexion()
        return self.fa

    # ------------------------------------------------------------ lecture

    def lire(self, chemin: str, donnees: dict | None = None, params: dict | None = None):
        for essai in range(2):
            if not self.token:
                self.connexion()
            rep = self._appel(chemin.format(eleve=self.eleve, boite=self.boite), donnees, params=params)
            if rep.get("code") == 200:
                return rep.get("data") or {}
            if rep.get("code") in (520, 525) and essai == 0:  # session expirée
                self.token = None
                continue
            raise ErreurEcoleDirecte(f"code {rep.get('code')}")
        return {}

    def emploi_du_temps(self, debut: str, fin: str) -> dict:
        data = self.lire("/E/{eleve}/emploidutemps.awp", {"dateDebut": debut, "dateFin": fin, "avecTrous": False})
        cours = {}
        for c in data or []:
            if not c.get("start_date") or not c.get("end_date"):
                continue
            cours[str(c.get("id"))] = {
                "matiere": joli(c.get("matiere") or c.get("text") or "Cours"),
                "salle": (c.get("salle") or "").strip(),
                "prof": (c.get("prof") or "").strip(),
                "debut": c["start_date"][:16],
                "fin": c["end_date"][:16],
                "annule": bool(c.get("isAnnule")),
                "modifie": bool(c.get("isModifie")),
            }
        return cours

    def notes(self) -> dict:
        data = self.lire("/eleves/{eleve}/notes.awp", {"anneeScolaire": ""})
        res = {}
        for n in data.get("notes", []) or []:
            cle = str(n.get("id") or f"{n.get('codeMatiere')}|{n.get('devoir')}|{n.get('date')}")
            res[cle] = {
                "matiere": joli(n.get("libelleMatiere")),
                "devoir": (n.get("devoir") or "").strip(),
                "valeur": (n.get("valeur") or "").strip(),
                "sur": (n.get("noteSur") or "").strip(),
                "coef": (n.get("coef") or "").strip(),
                "moyenne_classe": (n.get("moyenneClasse") or "").strip(),
            }
        return res

    def devoirs(self) -> dict:
        data = self.lire("/Eleves/{eleve}/cahierdetexte.awp")
        res = {}
        for date_rendu, liste in (data or {}).items():
            for d in liste or []:
                res[str(d.get("idDevoir"))] = {
                    "matiere": joli(d.get("matiere")),
                    "pour": str(date_rendu)[:10],
                    "controle": bool(d.get("interrogation")),
                }
        return res

    def consignes_du_jour(self, date_rendu: str) -> dict:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_rendu):
            return {}
        try:
            data = self.lire("/Eleves/{eleve}/cahierdetexte/" + date_rendu + ".awp")
            return {str(m.get("id")): texte_brut((m.get("aFaire") or {}).get("contenu"))
                    for m in data.get("matieres", []) or []}
        except (ErreurEcoleDirecte, requests.RequestException, ValueError, KeyError, AttributeError):
            return {}

    def messages(self, annee: str) -> dict:
        data = self.lire("{boite}/messages.awp", {"anneeMessages": annee}, params={
            "typeRecuperation": "received", "getAll": "1", "idClasseur": "0",
            "orderBy": "date", "order": "desc", "page": "0", "itemsPerPage": "100"})
        res = {}
        for m in ((data.get("messages") or {}).get("received") or []):
            exp = m.get("from") or {}
            nom = " ".join(x for x in (exp.get("civilite"), exp.get("prenom"), exp.get("nom")) if x).strip()
            res[str(m.get("id"))] = {"sujet": (m.get("subject") or "(sans objet)").strip(),
                                     "de": nom or exp.get("name") or "EcoleDirecte"}
        return res

    def vie_scolaire(self) -> dict:
        data = self.lire("/eleves/{eleve}/viescolaire.awp")
        res = {}
        for liste in ("absencesRetards", "sanctionsEncouragements"):
            for e in data.get(liste, []) or []:
                res[f"{liste}:{e.get('id')}"] = {
                    "type": (e.get("typeElement") or e.get("libelle") or "Événement").strip(),
                    "libelle": (e.get("libelle") or "").strip(),
                    "date": (e.get("displayDate") or e.get("date") or "").strip(),
                    "motif": (e.get("motif") or "").strip(),
                }
        return res
