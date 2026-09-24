# ferme-claude — POC

Faire tourner un agent sur **l'abonnement Claude personnel** de son propriétaire : un bac par personne,
connecté par elle-même, lancé sans surveillance. Besoin porté par client (lot 1), contrat backend
dans `oto-backend#1043`.

**Statut : POC validé le 23/09/2026 sur box-client (retiré depuis) ; agent posé en TEST sur box-agents le 24/09/2026.**
Pas un produit — voir « Ce qui manque ». Décision d'Alexis : aucun bac réel ni runner déplacé avant la bascule
de client sur son propre Scaleway.

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

## API (REST, Bearer, `FERME_HOST`:8190)

| méthode | route | effet |
| --- | --- | --- |
| GET | `/api/bacs/{slug}` | état (`claude auth status`) |
| PUT | `/api/bacs/{slug}` | crée le bac (idempotent) |
| DELETE | `/api/bacs/{slug}` | déconnecte et détruit |
| POST | `/api/bacs/{slug}/login` | démarre la connexion → `{url}` |
| PUT | `/api/bacs/{slug}/login/code` | transmet le code → état |
| POST | `/api/bacs/{slug}/runs` | `{prompt, model}` → résultat, `apiKeySource`, forfait, événements |

`GET /` sert `index.html`, la page qui joue le futur écran du dashboard.

## Déploiement (box-agents)

Box de test **box-agents** (DEV1-S, 2 Go → 2 runs de bac simultanés au plus), IP privée `10.0.0.2`.
Montage, réseau et socle : `<dépôt-infra>`, `scripts/box-agents/README.md`.

```
bash deploy/installer.sh box-agents 10.0.0.2
```

Idempotent : Node 22, Claude Code **épinglé** (`CLAUDE_CODE_VERSION` dans le script), code et unité,
`/etc/ferme/agent.env` (0600 : `FERME_TOKEN`, `FERME_HOST`) créé au premier passage seulement.

L'agent écoute sur l'IP privée ; nftables (table `ferme_pn`) n'y laisse entrer que oto-platform
(10.0.0.5). Aucun port public hors 22 et Beszel. Accès de test depuis un poste :
`ssh -N -L 8190:10.0.0.2:8190 box-agents` puis `http://localhost:8190`.

Test d'isolation, à rejouer depuis un vrai run après toute modification des brides :
`tests/isolation.js` posé dans `/usr/local/share/ferme/` (`/opt` est invisible du bac), puis
`python3 /opt/ferme/isolation.py <slug>` sur la box.

## Ce qui manque pour un produit

- Workers et agent non-root côté runner ; lecture des secrets depuis le Secret Manager Scaleway.
- L'agent derrière le backend (`#1043` : `upsert_sandbox`, `marquer_statut`, pool `claude_subscription`)
  et l'écran dans le dashboard.
- Une sonde `claude auth status` périodique → `needs_login`.
- Maîtriser la synchro des plugins du compte claude.ai dans le bac (scope `user:plugins`).
- **Conformité** : les conditions d'Anthropic couvrent l'utilisateur qui se connecte lui-même au binaire
  hébergé, pas clairement des runs ordonnancés d'agents d'org → confirmation écrite d'Anthropic avant
  toute ouverture.
