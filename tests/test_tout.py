"""Tests sans réseau : détection des changements, chiffrement, interface web, multi-comptes."""

import datetime as dt
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["COOKIE_SECURE"] = "false"

from ntfy_ecoledirecte import surveillance as S  # noqa: E402
from ntfy_ecoledirecte.securite import Coffre, empreinte_mdp, verifier_mdp  # noqa: E402
from ntfy_ecoledirecte.stockage import CONFIG_DEFAUT, Stockage  # noqa: E402
from ntfy_ecoledirecte.web import creer_app  # noqa: E402

INSTANT = dt.datetime(2026, 11, 4, 9, 56)


def cours(matiere, salle, debut, fin, annule=False, prof="M. Dupont"):
    return {"matiere": matiere, "salle": salle, "prof": prof, "debut": debut, "fin": fin, "annule": annule, "modifie": False}


class TestDetection(unittest.TestCase):
    def test_changements_de_cours(self):
        avant = {"1": cours("Maths", "B12", "2026-11-04 10:00", "2026-11-04 10:55"),
                 "2": cours("Anglais", "A04", "2026-11-04 11:00", "2026-11-04 11:55"),
                 "3": cours("SVT", "L2", "2026-11-04 14:00", "2026-11-04 14:55")}
        apres = {"1": cours("Maths", "B12", "2026-11-04 10:00", "2026-11-04 10:55", annule=True),
                 "2": cours("Anglais", "C07", "2026-11-04 11:00", "2026-11-04 11:55"),
                 "4": cours("EPS", "Gymnase", "2026-11-18 08:00", "2026-11-18 09:55")}
        titres = [a[0] for a in S.changements_cours(avant, apres, INSTANT, "2026-11-18")]
        self.assertEqual(titres, ["Cours annulé : Maths", "Changement de salle : Anglais", "Cours retiré : SVT"])

    def test_cours_passes_ignores(self):
        avant = {"1": cours("Maths", "B12", "2026-11-04 08:00", "2026-11-04 08:55")}
        apres = {"1": cours("Maths", "C01", "2026-11-04 08:00", "2026-11-04 08:55")}
        self.assertEqual(S.changements_cours(avant, apres, INSTANT), [])

    def test_rappels(self):
        c = {"1": cours("Maths", "B12", "2026-11-04 10:00", "2026-11-04 10:55"),
             "2": cours("SVT", "L2", "2026-11-04 10:00", "2026-11-04 10:55", annule=True)}
        self.assertEqual([k for k, _ in S.rappels_a_envoyer(c, set(), 5, INSTANT)], ["1@2026-11-04 10:00"])
        self.assertEqual(S.rappels_a_envoyer(c, {"1@2026-11-04 10:00"}, 5, INSTANT), [])
        self.assertEqual(S.rappels_a_envoyer(c, set(), 3, INSTANT), [])


class FausseSession:
    """Remplace la session EcoleDirecte : données modifiables par le test."""
    donnees = {}

    def __init__(self, *a, **k):
        self.token = None
        self.eleves = [{"id": "1", "prenom": "Alex"}]

    def connexion(self):
        self.token = "x"

    def emploi_du_temps(self, d, f):
        return dict(self.donnees["cours"])

    def notes(self):
        return dict(self.donnees["notes"])

    def devoirs(self):
        return {k: dict(v) for k, v in self.donnees["devoirs"].items()}

    def consignes_du_jour(self, d):
        return {"d1": "Réviser le chapitre 3."}

    def messages(self, annee):
        return dict(self.donnees["messages"])

    def vie_scolaire(self):
        return {}


class TestSurveillance(unittest.TestCase):
    def test_premiere_passe_silencieuse_puis_nouveautes_dans_le_bon_topic(self):
        envois = []
        FausseSession.donnees = {
            "cours": {"1": cours("Anglais", "A04", "2026-11-04 11:00", "2026-11-04 11:55")},
            "notes": {"n1": {"matiere": "SVT", "devoir": "DS", "valeur": "12", "sur": "20", "coef": "1", "moyenne_classe": ""}},
            "devoirs": {"d1": {"matiere": "Histoire", "pour": "2026-11-06", "controle": False}},
            "messages": {},
        }
        compte = {"id": 1, "nom": "Alex", "identifiant": "alex", "version": 1, "etat": {},
                  "secrets": {"motdepasse": "x"},
                  "config": {**CONFIG_DEFAUT, "topic_changements": "alex-chg", "topic_rappels": "alex-rap"}}
        with mock.patch.object(S, "Session", FausseSession), mock.patch.object(S, "maintenant", return_value=INSTANT):
            s = S.Surveillant(compte, {"jours_surveilles": 14}, lambda topic, titre, *a: envois.append((topic, titre)))
            s.verifier(notifier=False)
            self.assertEqual(envois, [])
            FausseSession.donnees["cours"]["1"] = cours("Anglais", "C07", "2026-11-04 11:00", "2026-11-04 11:55")
            FausseSession.donnees["notes"]["n2"] = {"matiere": "Physique", "devoir": "Contrôle", "valeur": "16",
                                                     "sur": "20", "coef": "2", "moyenne_classe": "12"}
            FausseSession.donnees["devoirs"]["d1"]["controle"] = True
            FausseSession.donnees["messages"]["9"] = {"sujet": "Sortie", "de": "M. Martin"}
            s.verifier()
            s.rappels()
        self.assertIn(("alex-chg", "Changement de salle : Anglais"), envois)
        self.assertIn(("alex-chg", "Nouvelle note en Physique : 16/20"), envois)
        self.assertIn(("alex-chg", "Contrôle prévu en Histoire"), envois)
        self.assertIn(("alex-chg", "Message de M. Martin"), envois)
        self.assertNotIn(("alex-chg", "Nouvelle note en SVT : 12/20"), envois)


class TestSecurite(unittest.TestCase):
    def test_mot_de_passe(self):
        h = empreinte_mdp("un mot de passe long")
        self.assertTrue(verifier_mdp("un mot de passe long", h))
        self.assertFalse(verifier_mdp("autre chose", h))
        self.assertFalse(verifier_mdp("x", None))

    def test_coffre(self):
        with tempfile.TemporaryDirectory() as d:
            c = Coffre(Path(d))
            jeton = c.chiffrer({"motdepasse": "secret"})
            self.assertNotIn("secret", jeton)
            self.assertEqual(c.dechiffrer(jeton), {"motdepasse": "secret"})
            self.assertEqual(oct((Path(d) / "cle-chiffrement").stat().st_mode & 0o777), "0o600")


class FauxMoteur:
    def reveiller(self, ident=None):
        pass


class TestWeb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stockage = Stockage(Path(self.tmp.name))
        with self.assertLogs("ntfy_ecoledirecte.web", "WARNING") as journaux:
            self.app = creer_app(self.stockage, FauxMoteur())
        self.code = re.search(r"code d'installation : (\S+)", "\n".join(journaux.output)).group(1)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def csrf(self, chemin):
        page = self.client.get(chemin).get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def installer(self):
        return self.client.post("/installation", data={"csrf": self.csrf("/installation"), "code": self.code,
                                                       "mdp": "un mot de passe solide", "mdp2": "un mot de passe solide"})

    def test_tout_est_protege_avant_installation(self):
        self.assertEqual(self.client.get("/").headers["Location"], "/installation")
        self.assertEqual(self.client.get("/parametres").headers["Location"], "/installation")

    def test_installation_exige_le_code(self):
        r = self.client.post("/installation", data={"csrf": self.csrf("/installation"), "code": "faux",
                                                     "mdp": "un mot de passe solide", "mdp2": "un mot de passe solide"})
        self.assertIn("incorrect", r.get_data(as_text=True))
        self.assertIsNone(self.stockage.reglage("admin_mdp"))
        self.assertEqual(self.installer().status_code, 302)
        self.assertTrue(self.stockage.reglage("admin_mdp").startswith("scrypt$"))

    def test_csrf_obligatoire(self):
        self.installer()
        r = self.client.post("/comptes/nouveau", data={"nom": "x"})
        self.assertEqual(r.status_code, 400)

    def test_en_tetes_de_securite(self):
        r = self.client.get("/connexion")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_ajout_de_comptes_et_chiffrement(self):
        self.installer()
        jeton = self.csrf("/comptes/nouveau")
        base = {"csrf": jeton, "identifiant": "alex", "motdepasse": "MotDePasseEcole!", "minutes_avant": "5",
                "actif": "on", "cat_notes": "on"}
        r = self.client.post("/comptes/nouveau", data={**base, "nom": "Alex", "topic_changements": "pas valide!"})
        self.assertIn("invalide", r.get_data(as_text=True))
        self.client.post("/comptes/nouveau", data={**base, "nom": "Alex", "topic_changements": "alex-chg", "topic_rappels": "alex-rap"})
        self.client.post("/comptes/nouveau", data={**base, "nom": "Sam", "identifiant": "sam", "topic_changements": "sam-chg"})
        comptes = self.stockage.comptes(avec_secrets=True)
        self.assertEqual([(c["nom"], c["config"]["topic_changements"]) for c in comptes], [("Alex", "alex-chg"), ("Sam", "sam-chg")])
        self.assertEqual(comptes[0]["secrets"]["motdepasse"], "MotDePasseEcole!")
        cnx = sqlite3.connect(self.stockage.fichier)
        brut = cnx.execute("SELECT group_concat(secrets) FROM comptes").fetchone()[0]
        cnx.close()
        self.assertNotIn("MotDePasseEcole", brut)
        page = self.client.get(f"/comptes/{comptes[0]['id']}").get_data(as_text=True)
        self.assertNotIn("MotDePasseEcole", page)

    def test_limite_des_essais(self):
        self.installer()
        self.client.post("/deconnexion", data={"csrf": self.csrf("/parametres")})
        with mock.patch("ntfy_ecoledirecte.web.time.sleep"):
            for _ in range(5):
                self.client.post("/connexion", data={"csrf": self.csrf("/connexion"), "mdp": "faux"})
            r = self.client.post("/connexion", data={"csrf": self.csrf("/connexion"), "mdp": "un mot de passe solide"})
        self.assertIn("Trop d", r.get_data(as_text=True))

    def test_changer_de_mot_de_passe_deconnecte_les_autres(self):
        self.installer()
        autre = self.app.test_client()
        autre.post("/connexion", data={"csrf": re.search(r'name="csrf" value="([^"]+)"', autre.get("/connexion").get_data(as_text=True)).group(1),
                                       "mdp": "un mot de passe solide"})
        self.assertEqual(autre.get("/").status_code, 200)
        self.client.post("/parametres", data={"csrf": self.csrf("/parametres"), "formulaire": "mdp",
                                              "actuel": "un mot de passe solide", "nouveau": "nouveau mot de passe 2",
                                              "nouveau2": "nouveau mot de passe 2"})
        self.assertEqual(autre.get("/").status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_question_de_securite(self):
        self.installer()
        ident = self.stockage.creer("Alex", "alex", "mdp", {**CONFIG_DEFAUT, "topic_changements": "t"})
        self.stockage.definir_statut(ident, "question")

        class FauxQCM:
            def __init__(self, *a, **k):
                pass

            def question_securite(self):
                return "Quel est votre mois de naissance ?", ["Mars", "Juin", "Octobre"]

            def repondre(self, i):
                if i != 1:
                    raise __import__("ntfy_ecoledirecte").ecoledirecte.ErreurEcoleDirecte("Mauvaise réponse")
                return {"cn": "a", "cv": "b"}

        with mock.patch("ntfy_ecoledirecte.web.Session", FauxQCM):
            page = self.client.get(f"/comptes/{ident}/securite").get_data(as_text=True)
            self.assertIn("mois de naissance", page)
            self.assertIn("Octobre", page)
            jeton = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
            r = self.client.post(f"/comptes/{ident}/securite", data={"csrf": jeton, "choix": "1"})
        self.assertEqual(r.status_code, 302)
        c = self.stockage.compte(ident, avec_secrets=True)
        self.assertEqual((c["statut"], c["secrets"]["fa"]), ("ok", {"cn": "a", "cv": "b"}))


if __name__ == "__main__":
    unittest.main()


class TestDemo(unittest.TestCase):
    def test_ecole_simulee_progresse(self):
        from ntfy_ecoledirecte import demo
        with mock.patch.object(demo, "ACTIF", True), mock.patch.object(demo.time, "time", return_value=1000.0):
            self.assertTrue(demo.est_demo("Demo"))
            s = demo.SessionDemo("demo-test-progression")
            self.assertEqual(len(s.notes()), 1)
        with mock.patch.object(demo.time, "time", return_value=1000.0 + 6 * demo.PAS):
            self.assertEqual(len(s.notes()), 2)
            self.assertTrue(any(c["annule"] for c in s.emploi_du_temps("", "").values()))
            self.assertTrue(s.vie_scolaire())

    def test_demo_desactivee_par_defaut(self):
        from ntfy_ecoledirecte import demo
        with mock.patch.object(demo, "ACTIF", False):
            self.assertFalse(demo.est_demo("demo"))


class TestDemoRappel(unittest.TestCase):
    def test_rappel_part_une_minute_apres_l_ajout(self):
        from ntfy_ecoledirecte import demo
        with mock.patch.object(demo, "_maintenant", return_value=INSTANT):
            cours = demo.SessionDemo("demo-test-rappel").emploi_du_temps("", "")
        self.assertEqual(S.rappels_a_envoyer(cours, set(), 5, INSTANT), [])
        rappels = S.rappels_a_envoyer(cours, set(), 5, INSTANT + dt.timedelta(minutes=1))
        self.assertEqual([c["matiere"] for _, c in rappels], ["Mathématiques"])
