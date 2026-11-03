# ntfy to EcoleDirecte

Reçois sur ton téléphone, via [ntfy](https://ntfy.sh), ce qui change sur **EcoleDirecte** — pour un ou **plusieurs comptes**, chacun avec ses propres topics.

- **Topic des changements** (uniquement les nouveautés) : cours annulé / prof absent, changement de salle, d'horaire ou de prof, cours ajouté ou retiré, nouvelle note, nouveau devoir ou **contrôle**, nouveau message, vie scolaire.
- **Topic des rappels** : « Dans 5 min : Mathématiques — Salle B12 · M. Dupont · 10h00–10h55 », avec un délai réglable par compte.
- **Interface web** pour ajouter des comptes, choisir les topics, répondre à la question de sécurité d'EcoleDirecte et régler ntfy.
- **Aucun port entrant nécessaire** : le service se connecte seulement à EcoleDirecte et à ton serveur ntfy.

> Projet indépendant, sans lien avec EcoleDirecte / Aplim. EcoleDirecte n'a pas d'API publique : le service utilise la même interface que le site web et peut cesser de fonctionner si elle change.

## Essayer la démo (sans compte EcoleDirecte)

Une école simulée et un serveur ntfy, tout compris :

```bash
git clone https://github.com/leochgn-collab/ntfy-to-ecoledirect.git
cd ntfy-to-ecoledirect
docker compose -f docker-compose.demo.yml up -d --build
docker compose -f docker-compose.demo.yml logs ntfy-to-ecoledirecte | grep "code d'installation"
```

1. Ouvre **http://127.0.0.1:8080**, saisis le code d'installation et choisis un mot de passe.
2. **Paramètres** › serveur ntfy : `http://ntfy:80`.
3. **Ajouter un compte** : identifiant `demo`, n'importe quel mot de passe, topics `demo-changements` et `demo-rappels`.
4. Ouvre **http://127.0.0.1:8090/demo-changements** et **http://127.0.0.1:8090/demo-rappels** dans deux onglets (ou abonne-toi depuis l'app ntfy).

Pendant les minutes qui suivent arrivent : un rappel avant un cours, un changement de salle, un prof absent, une note, un contrôle, un message et un retard. L'identifiant `demo-question` permet d'essayer la question de sécurité (réponse : « Mars »). Le mode démo ne s'active qu'avec `NTFY_ED_DEMO=true`.

Pour arrêter et tout effacer : `docker compose -f docker-compose.demo.yml down -v`.

## Démarrage rapide

```bash
git clone https://github.com/leochgn-collab/ntfy-to-ecoledirect.git
cd ntfy-to-ecoledirect

# Recommandé : une clé de chiffrement gardée hors du volume de données
echo "NTFY_ED_CLE=$(docker run --rm python:3.12-slim python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')" > .env
chmod 600 .env
# puis décommente la ligne NTFY_ED_CLE dans docker-compose.yml

docker compose up -d
docker compose logs ntfy-to-ecoledirecte | grep "code d'installation"
```

Ouvre ensuite **http://127.0.0.1:8080** sur la machine, saisis le **code d'installation** affiché dans les journaux et choisis le mot de passe de l'interface (12 caractères minimum).

Puis :

1. **Paramètres** : l'adresse de ton serveur ntfy (et son jeton s'il est protégé).
2. **Ajouter un compte** : identifiant et mot de passe EcoleDirecte, topic des changements, topic des rappels, ce qu'il faut surveiller.
3. Si le compte affiche **Question de sécurité**, clique sur le bouton et réponds (EcoleDirecte la pose pour chaque nouvel appareil).
4. **Tester ntfy** vérifie que les notifications arrivent dans les bons topics.

La première vérification d'un compte est silencieuse : seuls les changements suivants sont envoyés.

L'image est construite automatiquement pour `amd64` et `arm64` (Raspberry Pi) : `ghcr.io/leochgn-collab/ntfy-to-ecoledirect:latest`. Pour construire la tienne : `docker compose build`.

## Avec ton propre serveur ntfy

- Si ntfy tourne dans le même `docker compose`, mets les deux services sur le même réseau et utilise `http://ntfy:80` comme adresse.
- Pour des notifications **instantanées sur iPhone** depuis un serveur auto-hébergé, ton `server.yml` ntfy doit contenir :
  ```yaml
  upstream-base-url: "https://ntfy.sh"
  ```
- Serveur protégé : crée un jeton avec droit d'écriture sur les topics, par exemple
  `ntfy token add mon_utilisateur` puis `ntfy access mon_utilisateur 'ecole-*' write-only`.

## Plusieurs comptes

Ajoute autant de comptes que tu veux : chaque compte a ses topics, son délai de rappel et ses catégories. Pour un compte **parent** avec plusieurs enfants, ajoute le compte une fois par enfant et choisis l'élève dans le formulaire (la liste apparaît après la première connexion).

## Accéder à l'interface depuis l'extérieur

Par défaut l'interface n'écoute que sur `127.0.0.1`. Pour l'ouvrir ailleurs, **ne publie pas le port 8080 sur Internet** : place-la derrière un reverse proxy HTTPS (Caddy, Traefik, Nginx Proxy Manager, Cloudflare Tunnel…) et règle :

```yaml
COOKIE_SECURE: "true"
TRUST_PROXY: "true"   # seulement si le proxy est le seul à pouvoir joindre le conteneur
```

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `NTFY_ED_CLE` | (générée dans `/data`) | clé Fernet qui chiffre les mots de passe EcoleDirecte et les accès ntfy |
| `NTFY_ED_CLE_FICHIER` | — | chemin d'un fichier contenant la clé (secrets Docker) |
| `COOKIE_SECURE` | `true` | cookie de session réservé au HTTPS |
| `TRUST_PROXY` | `false` | lit l'IP et le protocole fournis par le reverse proxy |
| `TZ` | `Europe/Paris` | fuseau horaire des cours |
| `PORT` | `8080` | port interne |
| `LOG_LEVEL` | `INFO` | niveau des journaux |
| `NTFY_ED_DEMO` | `false` | active l'école simulée (comptes `demo` et `demo-question`) |
| `NTFY_ED_DEMO_PAS` | `60` | durée d'une étape de la démo, en secondes |

## Sécurité

Résumé des protections (détails dans [SECURITY.md](SECURITY.md)) :

- mots de passe EcoleDirecte et accès ntfy **chiffrés** dans la base, jamais réaffichés, jamais journalisés ;
- interface protégée par mot de passe (scrypt), installation protégée par un code à usage unique, **limitation des essais** ;
- jetons **CSRF** sur tous les formulaires, cookies `HttpOnly` + `SameSite=Strict`, en-têtes CSP / anti-iframe ;
- conteneur **non-root**, système de fichiers en lecture seule, aucune capacité Linux, `no-new-privileges` ;
- dépendances figées, mises à jour par Dependabot, analyse CodeQL et tests à chaque modification.

Aucun logiciel n'est garanti sans faille : si tu en trouves une, suis [SECURITY.md](SECURITY.md).

## Développement

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
NTFY_ED_DONNEES=./data COOKIE_SECURE=false python -m ntfy_ecoledirecte
```

Licence MIT.
