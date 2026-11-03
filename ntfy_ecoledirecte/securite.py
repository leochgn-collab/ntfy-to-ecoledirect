"""Chiffrement des secrets, empreintes de mots de passe et clés de l'application."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _fichier_secret(chemin: Path, generer) -> bytes:
    """Lit un secret depuis un fichier, ou le crée (droits 600) au premier lancement."""
    if chemin.exists():
        return chemin.read_bytes().strip()
    valeur = generer()
    # O_EXCL : ne jamais écraser un secret existant (deux démarrages simultanés).
    fd = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(valeur)
    return valeur


class Coffre:
    """Chiffre les mots de passe EcoleDirecte et jetons ntfy avant stockage (Fernet : AES-128-CBC + HMAC-SHA256).

    La clé vient de la variable d'environnement NTFY_ED_CLE (ou du fichier indiqué par
    NTFY_ED_CLE_FICHIER, pratique avec les secrets Docker). À défaut, elle est créée dans le
    dossier de données ; garde-la alors hors des sauvegardes de la base si tu le peux.
    """

    def __init__(self, dossier: Path):
        cle = os.environ.get("NTFY_ED_CLE", "").strip().encode()
        fichier_env = os.environ.get("NTFY_ED_CLE_FICHIER", "").strip()
        if not cle and fichier_env:
            cle = Path(fichier_env).read_bytes().strip()
        if not cle:
            cle = _fichier_secret(dossier / "cle-chiffrement", Fernet.generate_key)
        try:
            self._fernet = Fernet(cle)
        except (ValueError, TypeError) as e:
            raise SystemExit("NTFY_ED_CLE invalide : génère-la avec la commande indiquée dans le README.") from e

    def chiffrer(self, objet) -> str:
        return self._fernet.encrypt(json.dumps(objet).encode()).decode()

    def dechiffrer(self, jeton: str | None, defaut=None):
        if not jeton:
            return defaut
        try:
            return json.loads(self._fernet.decrypt(jeton.encode()))
        except (InvalidToken, ValueError):
            raise SystemExit("Impossible de déchiffrer les données : la clé de chiffrement a changé.")


def cle_sessions(dossier: Path) -> bytes:
    return _fichier_secret(dossier / "cle-sessions", lambda: secrets.token_hex(32).encode())


# ------------------------------------------------------------ mots de passe (scrypt)

_N, _R, _P = 2 ** 15, 8, 1


def empreinte_mdp(mdp: str) -> str:
    sel = secrets.token_bytes(16)
    h = hashlib.scrypt(mdp.encode(), salt=sel, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (_N, _R, _P, base64.b64encode(sel).decode(), base64.b64encode(h).decode())


def verifier_mdp(mdp: str, empreinte: str | None) -> bool:
    try:
        _, n, r, p, sel, h = (empreinte or "").split("$")
        calcul = hashlib.scrypt(mdp.encode(), salt=base64.b64decode(sel), n=int(n), r=int(r), p=int(p),
                                maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(calcul, base64.b64decode(h))
    except (ValueError, TypeError):
        # calcul factice pour garder un temps de réponse constant
        hashlib.scrypt(mdp.encode(), salt=b"0" * 16, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
        return False


def mdp_assez_fort(mdp: str) -> str | None:
    """Renvoie un message d'erreur, ou None si le mot de passe convient."""
    if len(mdp) < 12:
        return "Le mot de passe doit faire au moins 12 caractères."
    if mdp.lower() in {"motdepasse123", "password1234", "azertyuiop12", "123456789012"}:
        return "Ce mot de passe est trop courant."
    return None
