"""Envoi des notifications vers un serveur ntfy (ntfy.sh ou auto-hébergé)."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

MOTIF_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def topic_valide(t: str) -> bool:
    return bool(MOTIF_TOPIC.fullmatch(t or ""))


def url_ntfy_valide(url: str) -> bool:
    u = urlparse(url or "")
    return u.scheme in ("http", "https") and bool(u.netloc) and not u.username and not u.password


class ErreurNtfy(Exception):
    pass


def envoyer(reglages: dict, topic: str, titre: str, message: str, tags=None, priorite: int = 3) -> None:
    """Publie un message. `reglages` : {"serveur", "jeton", "utilisateur", "motdepasse"}."""
    if not topic_valide(topic):
        raise ErreurNtfy("nom de topic invalide")
    entetes, auth = {}, None
    if reglages.get("jeton"):
        entetes["Authorization"] = f"Bearer {reglages['jeton']}"
    elif reglages.get("utilisateur"):
        auth = (reglages["utilisateur"], reglages.get("motdepasse", ""))
    try:
        r = requests.post(reglages["serveur"].rstrip("/"), timeout=15, headers=entetes, auth=auth,
                          allow_redirects=False, json={
                              "topic": topic, "title": titre[:250], "message": (message or " ")[:3500],
                              "tags": list(tags or []), "priority": int(priorite)})
    except requests.RequestException as e:
        raise ErreurNtfy(f"serveur ntfy injoignable ({e.__class__.__name__})") from e
    if r.status_code in (401, 403):
        raise ErreurNtfy(f"le serveur ntfy refuse l'écriture dans « {topic} » (vérifie le jeton et les droits)")
    if r.status_code >= 300:
        raise ErreurNtfy(f"le serveur ntfy a répondu {r.status_code}")
