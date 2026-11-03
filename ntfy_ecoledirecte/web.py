"""Interface web d'administration (protégée par mot de passe)."""

from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
import threading
import time
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from . import notifications
from .ecoledirecte import ErreurEcoleDirecte, Session
from .securite import cle_sessions, empreinte_mdp, mdp_assez_fort, verifier_mdp
from .stockage import CATEGORIES, CONFIG_DEFAUT

log = logging.getLogger(__name__)


class Limiteur:
    """Limite les essais de mot de passe : par adresse IP et au total."""

    def __init__(self, par_ip=5, total=30, fenetre=900):
        self.par_ip, self.total, self.fenetre = par_ip, total, fenetre
        self.echecs: list[tuple[float, str]] = []
        self.verrou = threading.Lock()

    def _nettoyer(self):
        limite = time.time() - self.fenetre
        self.echecs = [e for e in self.echecs if e[0] > limite]

    def bloque(self, ip):
        with self.verrou:
            self._nettoyer()
            return len(self.echecs) >= self.total or sum(1 for _, i in self.echecs if i == ip) >= self.par_ip

    def echec(self, ip):
        with self.verrou:
            self.echecs.append((time.time(), ip))

    def reussite(self, ip):
        with self.verrou:
            self.echecs = [e for e in self.echecs if e[1] != ip]


def creer_app(stockage, moteur) -> Flask:
    app = Flask(__name__)
    securise = os.environ.get("COOKIE_SECURE", "true").lower() not in ("0", "false", "non", "no")
    app.config.update(
        SECRET_KEY=cle_sessions(stockage.dossier),
        SESSION_COOKIE_NAME="__Host-ned" if securise else "ned",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=securise,
        PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12),
        MAX_CONTENT_LENGTH=32 * 1024,
        TEMPLATES_AUTO_RELOAD=False,
    )
    if os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "oui", "yes"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    limiteur = Limiteur()
    questions: dict[int, tuple[Session, float]] = {}  # question de sécurité en cours, par compte

    # Code d'installation : affiché une seule fois dans les journaux du conteneur.
    code_installation = None
    if not stockage.reglage("admin_mdp"):
        code_installation = secrets.token_urlsafe(9)
        log.warning("=" * 60)
        log.warning("PREMIÈRE INSTALLATION — code d'installation : %s", code_installation)
        log.warning("Ouvre l'interface web et saisis ce code pour choisir ton mot de passe.")
        log.warning("=" * 60)

    # ------------------------------------------------------------ sécurité commune

    @app.after_request
    def entetes(rep):
        rep.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
        rep.headers["X-Content-Type-Options"] = "nosniff"
        rep.headers["X-Frame-Options"] = "DENY"
        rep.headers["Referrer-Policy"] = "no-referrer"
        rep.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), interest-cohort=()"
        rep.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if securise:
            rep.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.endpoint != "static":
            rep.headers["Cache-Control"] = "no-store"
        return rep

    def jeton_csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals["csrf"] = jeton_csrf

    @app.before_request
    def verifier_csrf():
        if request.method == "POST":
            attendu = session.get("csrf")
            recu = request.form.get("csrf", "")
            if not attendu or not secrets.compare_digest(attendu, recu):
                abort(400, "Formulaire expiré : recharge la page et réessaie.")

    def connecte():
        return (session.get("ok") is True
                and session.get("v") == stockage.reglage("admin_version", 0))

    def protege(vue):
        @wraps(vue)
        def enveloppe(*a, **k):
            if not stockage.reglage("admin_mdp"):
                return redirect(url_for("installation"))
            if not connecte():
                session.clear()
                return redirect(url_for("connexion"))
            return vue(*a, **k)
        return enveloppe

    def ouvrir_session():
        session.clear()
        session.permanent = True
        session["ok"] = True
        session["v"] = stockage.reglage("admin_version", 0)
        session["csrf"] = secrets.token_urlsafe(32)

    def compte_ou_404(ident):
        c = stockage.compte(ident)
        if not c:
            abort(404)
        return c

    # ------------------------------------------------------------ installation et connexion

    @app.get("/favicon.ico")
    def favicon():
        return redirect(url_for("static", filename="icone.svg"), 301)

    @app.get("/sante")
    def sante():
        return {"ok": True}

    @app.route("/installation", methods=["GET", "POST"])
    def installation():
        nonlocal code_installation
        if stockage.reglage("admin_mdp"):
            return redirect(url_for("connexion"))
        ip = request.remote_addr or "?"
        if request.method == "POST":
            if limiteur.bloque(ip):
                flash("Trop d'essais. Réessaie dans 15 minutes.", "erreur")
            elif not code_installation or not secrets.compare_digest(request.form.get("code", "").strip(), code_installation):
                limiteur.echec(ip)
                flash("Code d'installation incorrect (il est dans les journaux du conteneur).", "erreur")
            elif request.form.get("mdp") != request.form.get("mdp2"):
                flash("Les deux mots de passe ne sont pas identiques.", "erreur")
            elif (erreur := mdp_assez_fort(request.form.get("mdp", ""))):
                flash(erreur, "erreur")
            else:
                stockage.definir("admin_mdp", empreinte_mdp(request.form["mdp"]))
                stockage.definir("admin_version", 1)
                code_installation = None
                limiteur.reussite(ip)
                ouvrir_session()
                flash("Mot de passe enregistré. Configure maintenant ton serveur ntfy.", "ok")
                return redirect(url_for("parametres"))
        return render_template("installation.html")

    @app.route("/connexion", methods=["GET", "POST"])
    def connexion():
        if not stockage.reglage("admin_mdp"):
            return redirect(url_for("installation"))
        if connecte():
            return redirect(url_for("tableau"))
        ip = request.remote_addr or "?"
        if request.method == "POST":
            if limiteur.bloque(ip):
                flash("Trop d'essais. Réessaie dans 15 minutes.", "erreur")
            elif verifier_mdp(request.form.get("mdp", ""), stockage.reglage("admin_mdp")):
                limiteur.reussite(ip)
                ouvrir_session()
                return redirect(url_for("tableau"))
            else:
                limiteur.echec(ip)
                time.sleep(1)
                flash("Mot de passe incorrect.", "erreur")
        jeton_csrf()
        return render_template("connexion.html")

    @app.post("/deconnexion")
    def deconnexion():
        session.clear()
        return redirect(url_for("connexion"))

    # ------------------------------------------------------------ tableau de bord

    @app.get("/")
    @protege
    def tableau():
        return render_template("tableau.html", comptes=stockage.comptes(),
                               ntfy_ok=bool(stockage.reglage("ntfy_configure")))

    # ------------------------------------------------------------ comptes

    def lire_formulaire(creation: bool, existant: dict | None):
        f = request.form
        erreurs = []
        nom = f.get("nom", "").strip()
        identifiant = f.get("identifiant", "").strip()
        motdepasse = f.get("motdepasse", "")
        tc, tr = f.get("topic_changements", "").strip(), f.get("topic_rappels", "").strip()
        if not 1 <= len(nom) <= 60:
            erreurs.append("Donne un nom au compte (60 caractères maximum).")
        if not 1 <= len(identifiant) <= 100:
            erreurs.append("Indique l'identifiant EcoleDirecte.")
        if creation and not motdepasse:
            erreurs.append("Indique le mot de passe EcoleDirecte.")
        if len(motdepasse) > 200:
            erreurs.append("Mot de passe trop long.")
        for t in (tc, tr):
            if t and not notifications.topic_valide(t):
                erreurs.append(f"Topic « {t[:64]} » invalide : lettres, chiffres, - et _ uniquement (64 max).")
        if not tc and not tr:
            erreurs.append("Indique au moins un topic ntfy.")
        try:
            minutes = int(f.get("minutes_avant", 5))
            if not 1 <= minutes <= 60:
                raise ValueError
        except ValueError:
            erreurs.append("Le rappel doit être entre 1 et 60 minutes.")
            minutes = 5
        eleves = [e["id"] for e in ((existant or {}).get("etat", {}).get("eleves") or [])]
        eleve_id = f.get("eleve_id", "")
        config = {
            "topic_changements": tc, "topic_rappels": tr, "minutes_avant": minutes,
            "categories": {c: f.get(f"cat_{c}") == "on" for c in CATEGORIES},
            "eleve_id": eleve_id if eleve_id in eleves else "",
        }
        return erreurs, nom, identifiant, motdepasse, config, f.get("actif") == "on"

    @app.route("/comptes/nouveau", methods=["GET", "POST"])
    @protege
    def nouveau_compte():
        valeurs = {"nom": "", "identifiant": "", "config": dict(CONFIG_DEFAUT), "actif": True, "etat": {}}
        if request.method == "POST":
            erreurs, nom, identifiant, mdp, config, actif = lire_formulaire(True, None)
            if not erreurs:
                ident = stockage.creer(nom, identifiant, mdp, config, actif)
                moteur.reveiller()
                flash(f"Compte « {nom} » ajouté. Première vérification en cours…", "ok")
                return redirect(url_for("tableau", _anchor=f"c{ident}"))
            for e in erreurs:
                flash(e, "erreur")
            valeurs = {"nom": nom, "identifiant": identifiant, "config": config, "actif": actif, "etat": {}}
        return render_template("compte.html", c=valeurs, creation=True, categories=CATEGORIES)

    @app.route("/comptes/<int:ident>", methods=["GET", "POST"])
    @protege
    def modifier_compte(ident):
        c = compte_ou_404(ident)
        if request.method == "POST":
            erreurs, nom, identifiant, mdp, config, actif = lire_formulaire(False, c)
            if not erreurs:
                stockage.modifier(ident, nom, identifiant, mdp, config, actif)
                moteur.reveiller()
                flash("Modifications enregistrées.", "ok")
                return redirect(url_for("tableau"))
            for e in erreurs:
                flash(e, "erreur")
            c = {**c, "nom": nom, "identifiant": identifiant, "config": config, "actif": actif}
        return render_template("compte.html", c=c, creation=False, categories=CATEGORIES)

    @app.route("/comptes/<int:ident>/supprimer", methods=["GET", "POST"])
    @protege
    def supprimer_compte(ident):
        c = compte_ou_404(ident)
        if request.method == "POST":
            stockage.supprimer(ident)
            questions.pop(ident, None)
            moteur.reveiller()
            flash(f"Compte « {c['nom']} » supprimé, avec son mot de passe.", "ok")
            return redirect(url_for("tableau"))
        return render_template("supprimer.html", c=c)

    @app.post("/comptes/<int:ident>/tester")
    @protege
    def tester_compte(ident):
        c = compte_ou_404(ident)
        reglages = stockage.reglages_ntfy()
        for salon, titre in (("topic_changements", "Test : topic des changements"), ("topic_rappels", "Test : topic des rappels")):
            topic = c["config"].get(salon)
            if not topic:
                continue
            try:
                notifications.envoyer(reglages, topic, titre, f"Compte « {c['nom']} » : les notifications arrivent bien ici.", ["bell"])
                flash(f"Notification de test envoyée dans « {topic} ».", "ok")
            except notifications.ErreurNtfy as e:
                flash(f"« {topic} » : {e}.", "erreur")
        return redirect(url_for("tableau", _anchor=f"c{ident}"))

    @app.post("/comptes/<int:ident>/verifier")
    @protege
    def verifier_compte(ident):
        compte_ou_404(ident)
        moteur.reveiller(ident)
        flash("Vérification lancée. Recharge la page dans quelques secondes.", "ok")
        return redirect(url_for("tableau", _anchor=f"c{ident}"))

    @app.route("/comptes/<int:ident>/securite", methods=["GET", "POST"])
    @protege
    def securite(ident):
        c = stockage.compte(ident, avec_secrets=True)
        if not c:
            abort(404)
        if request.method == "POST":
            en_cours = questions.pop(ident, None)
            if not en_cours or en_cours[1] < time.time():
                flash("La question a expiré, en voici une nouvelle.", "erreur")
                return redirect(url_for("securite", ident=ident))
            try:
                fa = en_cours[0].repondre(int(request.form.get("choix", "-1")))
            except (ErreurEcoleDirecte, ValueError) as e:
                flash(str(e), "erreur")
                return redirect(url_for("securite", ident=ident))
            except Exception:
                log.exception("question de sécurité")
                flash("EcoleDirecte ne répond pas. Réessaie dans un moment.", "erreur")
                return redirect(url_for("securite", ident=ident))
            stockage.enregistrer_fa(ident, fa)
            moteur.reveiller(ident)
            flash("Réponse acceptée : la surveillance reprend.", "ok")
            return redirect(url_for("tableau", _anchor=f"c{ident}"))

        s = Session(c["identifiant"], c["secrets"].get("motdepasse", ""))
        try:
            question, propositions = s.question_securite()
        except ErreurEcoleDirecte as e:
            flash(str(e), "erreur")
            return redirect(url_for("tableau"))
        except Exception:
            log.exception("question de sécurité")
            flash("EcoleDirecte ne répond pas. Réessaie dans un moment.", "erreur")
            return redirect(url_for("tableau"))
        questions[ident] = (s, time.time() + 300)
        return render_template("securite.html", c=c, question=question, propositions=propositions)

    # ------------------------------------------------------------ paramètres

    @app.route("/parametres", methods=["GET", "POST"])
    @protege
    def parametres():
        if request.method == "POST" and request.form.get("formulaire") == "ntfy":
            f = request.form
            serveur = f.get("ntfy_serveur", "").strip().rstrip("/")
            erreurs = []
            if not notifications.url_ntfy_valide(serveur):
                erreurs.append("Adresse du serveur ntfy invalide (http:// ou https://, sans identifiants dedans).")
            try:
                verif = int(f.get("verif_minutes", 10))
                jours = int(f.get("jours_surveilles", 14))
                if not (5 <= verif <= 120 and 1 <= jours <= 30):
                    raise ValueError
            except ValueError:
                erreurs.append("Fréquence entre 5 et 120 minutes, fenêtre entre 1 et 30 jours.")
            if erreurs:
                for e in erreurs:
                    flash(e, "erreur")
            else:
                stockage.definir("ntfy_serveur", serveur)
                stockage.definir("verif_minutes", verif)
                stockage.definir("jours_surveilles", jours)
                mode = f.get("auth", "aucune")
                if mode == "aucune":
                    stockage.definir_secret("ntfy_jeton", None)
                    stockage.definir("ntfy_utilisateur", "")
                    stockage.definir_secret("ntfy_motdepasse", None)
                elif mode == "jeton":
                    if f.get("ntfy_jeton"):
                        stockage.definir_secret("ntfy_jeton", f["ntfy_jeton"].strip()[:200])
                    stockage.definir("ntfy_utilisateur", "")
                    stockage.definir_secret("ntfy_motdepasse", None)
                elif mode == "utilisateur":
                    stockage.definir_secret("ntfy_jeton", None)
                    stockage.definir("ntfy_utilisateur", f.get("ntfy_utilisateur", "").strip()[:100])
                    if f.get("ntfy_motdepasse"):
                        stockage.definir_secret("ntfy_motdepasse", f["ntfy_motdepasse"][:200])
                stockage.definir("ntfy_configure", True)
                moteur.reveiller()
                flash("Réglages ntfy enregistrés.", "ok")
                return redirect(url_for("tableau"))

        if request.method == "POST" and request.form.get("formulaire") == "mdp":
            f = request.form
            if not verifier_mdp(f.get("actuel", ""), stockage.reglage("admin_mdp")):
                flash("Mot de passe actuel incorrect.", "erreur")
            elif f.get("nouveau") != f.get("nouveau2"):
                flash("Les deux nouveaux mots de passe ne sont pas identiques.", "erreur")
            elif (erreur := mdp_assez_fort(f.get("nouveau", ""))):
                flash(erreur, "erreur")
            else:
                stockage.definir("admin_mdp", empreinte_mdp(f["nouveau"]))
                stockage.definir("admin_version", int(stockage.reglage("admin_version", 0)) + 1)
                ouvrir_session()  # les autres sessions ouvertes sont déconnectées
                flash("Mot de passe changé. Les autres appareils ont été déconnectés.", "ok")
                return redirect(url_for("tableau"))

        n = stockage.reglages_ntfy()
        auth = "jeton" if n["jeton"] else "utilisateur" if n["utilisateur"] else "aucune"
        return render_template("parametres.html", serveur=n["serveur"], auth=auth, utilisateur=n["utilisateur"],
                               a_jeton=bool(n["jeton"]), a_mdp=bool(n["motdepasse"]),
                               verif=stockage.reglage("verif_minutes"), jours=stockage.reglage("jours_surveilles"))

    # ------------------------------------------------------------ affichage

    @app.template_filter("depuis")
    def depuis(iso):
        if not iso:
            return "jamais"
        try:
            d = dt.datetime.fromisoformat(iso)
        except ValueError:
            return iso
        from .surveillance import maintenant
        minutes = int((maintenant() - d).total_seconds() // 60)
        if minutes < 1:
            return "à l'instant"
        if minutes < 60:
            return f"il y a {minutes} min"
        if minutes < 24 * 60:
            return f"il y a {minutes // 60} h"
        return d.strftime("le %d/%m à %Hh%M")

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def erreur(e):
        return render_template("erreur.html", code=e.code, message=getattr(e, "description", "")), e.code

    return app
