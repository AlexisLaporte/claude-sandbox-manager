# ferme-claude — POC

Faire tourner un agent sur **l'abonnement Claude personnel** de son propriétaire : un bac par personne,
connecté par elle-même, lancé sans surveillance. Besoin porté par client (lot 1), contrat backend
dans `oto-backend#1043`.

**Statut : POC validé le 23/09/2026 sur box-client.** Pas un produit — voir « Ce qui manque ».

## Ce que ça prouve

- Un compte Max connecté depuis une page web (pas de terminal) ; un `claude -p` sans surveillance tourne
  dessus : `apiKeySource: none`, 8 s, forfait remonté (`rate_limit_event` : fenêtres 5 h et 7 j).
- Le jeton ne quitte jamais le bac : la page ne relaie qu'un code OAuth à usage unique, inutilisable hors
  du CLI qui détient le vérifieur PKCE.
- Empreinte : ~0,5 Mo au repos, ~150 Mo et ~13 % d'un CPU par run ; Claude Code = 227 Mo de disque.
- Jeton d'accès ~8 h, renouvelé seul par le CLI (rotation du refresh) ; refresh ~4 semaines → un bac
  inactif plus longtemps doit se reconnecter.

## Comment c'est fait

- **Un bac = un utilisateur Unix** `bac-<slug>`, HOME `/srv/ferme/bacs/<slug>` (0700, racine 0711).
  Pas de conteneur : box-client n'a ni Docker ni la place.
- **Chaque commande = une unité `systemd-run` éphémère bridée** (`BRIDES` dans `agent.py`) :
  500 Mo, 80 % CPU, système en lecture seule sauf son HOME, `/opt`, `/etc/ferme` et la base invisibles,
  localhost et réseaux privés interdits sauf le résolveur DNS (127.0.0.53/54).
- **Connexion** : `claude auth login --claudeai` sans TTY écrit l'URL sur stdout et lit le code sur stdin ;
  l'agent garde le processus en attente dans l'unité `ferme-login-<slug>`.
- **Un run à la fois par bac** (verrou) : deux refresh simultanés, chacun faisant tourner le refresh
  token, casseraient la session.
- **Destruction** : `claude auth logout`, `pkill` des processus du bac, `userdel --remove`.

## API (REST, Bearer, 127.0.0.1:8190)

| méthode | route | effet |
| --- | --- | --- |
| GET | `/api/bacs/{slug}` | état (`claude auth status`) |
| PUT | `/api/bacs/{slug}` | crée le bac (idempotent) |
| DELETE | `/api/bacs/{slug}` | déconnecte et détruit |
| POST | `/api/bacs/{slug}/login` | démarre la connexion → `{url}` |
| PUT | `/api/bacs/{slug}/login/code` | transmet le code → état |
| POST | `/api/bacs/{slug}/runs` | `{prompt, model}` → résultat, `apiKeySource`, forfait, événements |

`GET /` sert `index.html`, la page qui joue le futur écran du dashboard.

## Déploiement (box-client)

```
npm install -g @anthropic-ai/claude-code
scp agent.py index.html root@203.0.113.10:/opt/ferme/
scp deploy/ferme-agent.service root@203.0.113.10:/etc/systemd/system/
# /etc/ferme/agent.env (0600) : FERME_TOKEN=<openssl rand -hex 24>
systemctl daemon-reload && systemctl enable --now ferme-agent
```

Accès : `ssh -N -L 8190:127.0.0.1:8190 root@203.0.113.10` puis `http://localhost:8190`.
Aucun vhost Caddy ni DNS : la box et sa config appartiennent à client (`client-team/infra`).

Test d'isolation : `tests/isolation.py <slug>` lance `tests/isolation.js` (à poser dans
`/usr/local/share/ferme/`, `/opt` étant invisible du bac) avec exactement les brides de l'agent.

## Ce qui manque pour un produit

- Une box dédiée : box-client a ~760 Mo libres, sans swap, à côté de la prod client.
- L'agent derrière le backend (`#1043` : `upsert_sandbox`, `marquer_statut`, pool `claude_subscription`)
  et l'écran dans le dashboard.
- Une sonde `claude auth status` périodique → `needs_login`.
- Maîtriser la synchro des plugins du compte claude.ai dans le bac (scope `user:plugins`).
- **Conformité** : les conditions d'Anthropic couvrent l'utilisateur qui se connecte lui-même au binaire
  hébergé, pas clairement des runs ordonnancés d'agents d'org → confirmation écrite d'Anthropic avant
  toute ouverture.
