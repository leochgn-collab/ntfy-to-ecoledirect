"""Point d'entrée : `python -m ntfy_ecoledirecte`."""

import logging
import os
import sys
from pathlib import Path

from .stockage import Stockage
from .surveillance import Moteur
from .web import creer_app


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s : %(message)s", stream=sys.stdout)
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)
    dossier = Path(os.environ.get("NTFY_ED_DONNEES", "/data"))
    dossier.mkdir(parents=True, exist_ok=True)

    stockage = Stockage(dossier)
    moteur = Moteur(stockage)
    app = creer_app(stockage, moteur)
    moteur.demarrer()

    hote = os.environ.get("HOTE", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    try:
        from waitress import serve
    except ImportError:  # développement uniquement
        logging.warning("waitress absent : serveur de développement Flask (ne pas utiliser en production)")
        app.run(host=hote, port=port)
        return
    logging.info("Interface web sur le port %s", port)
    serve(app, host=hote, port=port, threads=8, ident="ntfy-to-ecoledirecte",
          trusted_proxy=None, clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
