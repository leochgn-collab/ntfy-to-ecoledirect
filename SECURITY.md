# Sécurité

## Signaler une faille

N'ouvre pas d'issue publique. Utilise **Security › Report a vulnerability** sur ce dépôt (signalement privé GitHub). Décris le problème et comment le reproduire ; une réponse est donnée dès que possible.

## Ce que le service protège, et comment

| Risque | Protection |
|---|---|
| Vol de la base de données | Mots de passe EcoleDirecte, preuve de la question de sécurité et accès ntfy chiffrés (Fernet). Garde `NTFY_ED_CLE` hors du volume de données pour qu'une copie de la base seule soit inutilisable. |
| Prise de contrôle à l'installation | Le premier mot de passe ne peut être choisi qu'avec un code aléatoire affiché uniquement dans les journaux du conteneur. |
| Devinette du mot de passe | scrypt (N=2^15), 12 caractères minimum, 5 essais par IP et 30 au total par 15 minutes. |
| CSRF / clickjacking | Jeton CSRF sur chaque formulaire, `SameSite=Strict`, `frame-ancestors 'none'`, `X-Frame-Options: DENY`. |
| XSS | Échappement automatique des gabarits, CSP sans script (`default-src 'none'`). |
| Vol de session | Cookie `HttpOnly`, `Secure` (préfixe `__Host-`) derrière HTTPS, expiration 12 h, sessions invalidées au changement de mot de passe. |
| Fuite dans les journaux | Aucun identifiant, mot de passe ni jeton n'est journalisé. |
| Compromission du conteneur | Utilisateur non-root (uid 10001), racine en lecture seule, aucune capacité, `no-new-privileges`, limites mémoire et processus. |
| Exposition réseau | Port publié sur `127.0.0.1` uniquement ; accès distant prévu via reverse proxy HTTPS. |
| Dépendances vulnérables | Versions figées, Dependabot, CodeQL hebdomadaire. |

## Limites connues

- Le service doit pouvoir se reconnecter à EcoleDirecte : il conserve donc les mots de passe sous forme **déchiffrable** (avec la clé). Quiconque obtient à la fois la base et la clé peut les lire.
- La limitation des essais globale peut bloquer temporairement l'administrateur si quelqu'un attaque massivement l'interface : ne l'expose pas sans reverse proxy.
- Les topics ntfy publics sans authentification sont lisibles par quiconque devine leur nom : utilise un serveur protégé ou des noms difficiles à deviner.
