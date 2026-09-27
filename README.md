# claude-sandbox-manager

Faire tourner un agent sur **l'abonnement Claude personnel** de son propriétaire : un sandbox par personne,
connecté par elle-même, lancé sans surveillance. Besoin porté par client (lot 1), contrat backend
dans `oto-backend#1043`.

**Statut (24/09/2026) : en PROD, ouvert à trois personnes nommées** (Alexis, Julien, un collègue — option
`claude_subscription`). Box `box-agents`, un worker `claude-subscription`. Premier run de bout en bout le 24/09
(travail 86093 : `oto_procedure` + `oto_whoami` sur l'abonnement, forfait remonté). Ouverture au-delà : après
confirmation écrite d'Anthropic (voir « Ce qui manque »).

Les pièces : cet agent (sandboxes), `oto-runner` (moteur `claude-subscription`, relais MCP), `oto-backend`
(`/api/me/model-subscriptions`, option, gardes, `oto_mcp/ferme.py`), `oto-dashboard` (`/account/claude`).

## Ce que ça prouve

- Un compte Max connecté depuis une page web (pas de terminal) ; un `claude -p` sans surveillance tourne
  dessus : `apiKeySource: none`, 8 s, forfait remonté (`rate_limit_event` : fenêtres 5 h et 7 j).
- Le jeton ne quitte jamais le sandbox : la page ne relaie qu'un code OAuth à usage unique, inutilisable hors
  du CLI qui détient le vérifieur PKCE.
- Empreinte : ~0,5 Mo au repos, ~150 Mo et ~13 % d'un CPU par run ; Claude Code = 227 Mo de disque.
- Jeton d'accès ~8 h, renouvelé seul par le CLI (rotation du refresh) ; refresh ~4 semaines → un sandbox
  inactif plus longtemps doit se reconnecter.

## Comment c'est fait

- **Un sandbox = un utilisateur Unix** `sandbox-<slug>`, HOME `/srv/sandboxes/<slug>` (0700, racine 0711).
  Pas de conteneur : box-client n'a ni Docker ni la place.
- **Chaque commande = une unité `systemd-run` éphémère bridée** (`BRIDES` dans `agent.py`) :
  500 Mo, 80 % CPU, système en lecture seule sauf son HOME, `/opt`, `/etc/ferme` et la base invisibles,
  localhost et réseaux privés interdits sauf le résolveur DNS (127.0.0.53/54).
- **Connexion** : `claude auth login --claudeai` sans TTY écrit l'URL sur stdout et lit le code sur stdin ;
  l'agent garde le processus en attente dans l'unité `sandbox-login-<slug>`.
- **Un run à la fois par sandbox** (verrou) : deux refresh simultanés, chacun faisant tourner le refresh
  token, casseraient la session.
- **Destruction** : `claude auth logout`, `pkill` des processus du sandbox, `userdel --remove`.

## API (REST, Bearer, `FERME_HOST`:8190)

| méthode | route | effet |
| --- | --- | --- |
| GET | `/api/sandboxes/{slug}` | état : la session (`claude auth status`) + `apiKey` (une clé est posée) |
| PUT | `/api/sandboxes/{slug}` | crée le sandbox (idempotent) |
| DELETE | `/api/sandboxes/{slug}` | déconnecte et détruit |
| POST | `/api/sandboxes/{slug}/login` | démarre la connexion → `{url}` |
| PUT | `/api/sandboxes/{slug}/login/code` | transmet le code → état |
| PUT | `/api/sandboxes/{slug}/key` | pose (ou remplace) une clé d'API `{api_key}` → état |
| DELETE | `/api/sandboxes/{slug}/key` | retire la clé → état |
| POST | `/api/sandboxes/{slug}/runs` | `{prompt, model, system?, mcp?, auth?, api_key?, workspace?, effort?, max_seconds?, max_turns?, max_budget_usd?}` → résumé ; flux NDJSON avec `Accept: application/x-ndjson` ; 429 quand la box est pleine |

`mcp` = `{url, token, org, project, run_id, tools}` : le run n'a alors que deux outils intégrés, la délégation à un
**sous-agent** (`Task` : même processus, même unité, même clé, même relais) et `Read` borné (ci-dessous), et les outils du relais MCP
d'oto-runner (`oto_runner.relais_mcp`), lancé dans le sandbox par le CLI. Un run attend ses sous-agents
(`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`) : la durée de l'unité est sa seule borne de temps. Le relais pose
`_org`/`_project`/`_run_id` et refuse tout outil hors de `tools`. Le flux se termine par un
`ferme_resume` qui dit, en cas d'échec, si le sandbox est encore connecté.

**`Read` borné** (runs à relais) : il ne sert qu'à relire un gros résultat d'outil que le CLI
a rangé dans un fichier (au-delà de `MAX_MCP_OUTPUT_TOKENS=10000`, un enregistrement par
ligne, lu par `offset`/`limit`). Trois gestes vont ensemble, et aucun ne suffit seul :
- le run tourne dans un **répertoire vide qui lui est propre** (`~/.ferme-travail/<session>`),
  jamais dans le HOME : le CLI lit son répertoire de travail SANS règle, et le HOME porte la
  session claude.ai et la config MCP (le jeton du travail) ;
- `Read` n'est permis que sur `<config>/projects/<projet du répertoire>/<session>/tool-results/**`
  (`--session-id` fixe la session ; `<config>` = `~/.claude`, ou le `CLAUDE_CONFIG_DIR` du run
  par clé) — ni les autres runs, ni les transcripts ;
- refus explicites en seconde ceinture : `.credentials.json`, `~/.claude.json`,
  `.ferme-mcp-*.json`, `/proc/**`.
Le répertoire et le dossier que le CLI y associe (transcript, résultats : des données du
client) sont effacés à la fin du run — avant la config du run par clé qui les porte — et au
démarrage de l'agent. Comme `.ferme-runs`, `.ferme-travail` et `.claude/projects` sont
ouverts sans suivre de lien : création, `chown` et effacement passent par descripteur.
⚠️ Un résultat de 10 000 à 25 000 jetons, qui restait jusqu'ici dans le contexte, part
désormais dans un fichier : un tour de `Read` de plus pour le relire.

### Deux connexions : session et clé d'API

Un sandbox peut porter une **session claude.ai** (`/login`, faite par la personne elle-même), une **clé
d'API** de la Console (`/key`), ou les deux. Chaque run dit ce qui le paie : `auth: "key"` ou
`auth: "session"`. **Sans `auth`, c'est la session** : poser une clé ne fait jamais basculer les runs d'un
appelant qui ne la demande pas (le moteur `claude-subscription` d'oto-runner n'envoie pas `auth`).
`auth: "key"` sans clé posée → 409.

- **Où vit la clé** : `/etc/ferme/cles/<slug>` (root, 0600, `FERME_CLES`), **hors du sandbox**, dans un
  répertoire que les brides rendent invisible. Elle n'entre que dans l'environnement du run, par
  `EnvironmentFile=` de l'unité — jamais en argv (lisible de tous dans `/proc`), jamais dans le HOME
  (systemd lit ce fichier en root et suit les liens : un sandbox pourrait y substituer un lien vers un
  secret de la box).
- **Ce qui est refusé** : tout ce qui n'est pas `sk-ant-api…` (422), et nommément un jeton de session
  `sk-ant-oat…` — intermédier un identifiant claude.ai est interdit par Anthropic ; une session se
  connecte par `/login`.
- **Le CLI doit confirmer** : `system/init.apiKeySource` doit valoir `ANTHROPIC_API_KEY` pour un run par
  clé, `none` pour un run par session. Sinon (précédence inattendue, `apiKeyHelper` posé dans le HOME du
  sandbox…) le run est **arrêté avant tout tour** et `ferme_resume.erreur` commence par `auth_mismatch`.
- `ferme_resume` porte `auth` ; en cas d'échec, `connecte` dit si ce qui payait est encore là (session
  toujours connectée, clé toujours posée). Une clé **refusée** par l'API reste « posée » : le refus est dans
  `erreur`, pas un `needs_login`.
- **Pour les consommateurs** : un sandbox à clé seule répond `loggedIn: false` — lire `apiKey`, pas
  seulement `loggedIn`, avant de conclure qu'il faut se reconnecter.
- **La clé remise avec le run** (`api_key`, et `workspace` pour une clé d'organisation
  Anthropic, qui part en `ANTHROPIC_CUSTOM_HEADERS`) : c'est le chemin du worker, qui
  reçoit la clé de l'org avec chaque travail. Elle est écrite dans `CLES_RUN/<slug>.run-<id>`
  (`/run/ferme/cles-run`, **en mémoire** — tmpfs, jamais le disque persistant ; root 0700/0600,
  invisible aux runs), servie par `EnvironmentFile=`, **effacée à la fin du run** et au
  démarrage de l'agent : aucune copie ne reste sur la box, et la rotation dans le coffre vaut au run suivant. `PUT /key` reste
  pour une connexion à la main.
- **Un run de SESSION à la fois par sandbox** (le verrou protège le rafraîchissement du
  jeton OAuth). **Les runs par clé n'ont pas ce verrou** : chacun a son unité
  (`sandbox-run-<slug>-<id>`) et son `CLAUDE_CONFIG_DIR` (`~/.ferme-runs/<id>`, effacé à la
  fin) — plusieurs tournent en parallèle dans le sandbox d'une org. ⚠️ Ce HOME appartient à
  la personne et l'agent tourne en root : `.ferme-runs` est ouvert SANS suivre de lien
  (`O_NOFOLLOW`), et tout ce qui s'y crée, s'y `chown` ou s'y efface passe par ce
  descripteur — un lien posé à la place n'est jamais suivi, ni à la création ni au ménage.
- **Un corps de run incomplet** (sans `prompt`) est refusé en 400 **avant** toute réservation.
- **Places de la box** : `FERME_RUNS_MAX` runs en vol (défaut 3), tous modes confondus ;
  au-delà, 429. Un refus avant réservation ne garde rien (place, fichier, dossier).
- `effort` → `--effort` du CLI (`none`, valeur du catalogue d'oto pour un modèle qui ne
  raisonne pas, ne part pas).

**Durée d'un run** : `max_seconds` (la durée déclarée sur l'agent, oto-backend) devient le
`RuntimeMaxSec` de l'unité, borné à 60–3600 s ; sans lui, les 1800 s des brides. Le worker
tient la même échéance sur le flux et ferme la connexion quand elle tombe — ce qui arrête
déjà l'unité (le `finally` du flux) : la durée de l'unité est le filet, pas le geste.

**Bornes d'un run** (tous les runs) : `max_turns` → `--max-turns` (défaut 200, borné à 1–1000) et
`max_budget_usd` → `--max-budget-usd` (défaut 5 $, borné à 0,1–50 $), surchargeables par run comme
`max_seconds`. 200 tours, c'est ce que tiennent les 1800 s des brides à ~9 s le tour : la durée reste la
borne d'un run normal, les tours arrêtent une boucle rapide. 5 $ : de l'ordre d'un million de jetons
d'entrée non cachés en Sonnet, large pour un run de procédure ; un agent qui tourne sur Opus le déclare.
Une borne atteinte finit le run en `error_max_turns` / `error_max_budget_usd`, que `ferme_resume.erreur`
reprend. Les sous-agents tournent sur le modèle du run (`CLAUDE_CODE_SUBAGENT_MODEL=inherit` et
`CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` : le paramètre `model` de `Task` est retiré), jamais sur un modèle
plus cher choisi par le modèle.

Tests hors box : `python3 -m unittest discover -s tests -p 'test_*.py'`. **À vérifier sur la box** avant
usage : `systemd-run -p EnvironmentFile=…` accepté pour une unité éphémère et lu malgré
`InaccessiblePaths=/etc/ferme` et `/run/ferme` ; `ProtectProc=invisible` accepté par l'unité éphémère,
et un run ne voit plus le `cmdline` des runs d'une autre org ; aucun `sk-ant-api` dans
`ps`/`/proc/*/cmdline` pendant un run ; sur un
sandbox qui a les deux, un run `key` annonce `ANTHROPIC_API_KEY` et un run `session` annonce `none`.
**Avant de servir `Read`** : `python3 tests/banc_read.py /usr/bin/claude` sur la box, en root puis
en `sandbox-*` (sortie 0 = conforme ; passé sur macOS / CLI 2.1.281 — `/proc/self/environ`, qui
porte la clé d'un run par clé, n'est pas encore prouvé refusé sous Linux). Et un sous-agent
(`claude-code-guide`, `general-purpose`) à qui l'on fait tenter Bash puis WebFetch est refusé. Un run
qui délègue voit son sous-agent sur le modèle du run (`modelUsage` du `result`), et un `max_budget_usd`
bas l'arrête en `error_max_budget_usd` sous-agents compris.

`GET /` sert `index.html`, la page qui joue le futur écran du dashboard.

## Déploiement

**Procédure complète, de la box nue aux premières personnes : [`DEPLOIEMENT.md`](DEPLOIEMENT.md).**
Scripts : `deploy/box/box.sh` (pare-feu du réseau privé, socle, sonde), `deploy/secrets.sh` (secrets lus
au démarrage dans le Secret Manager), `deploy/oto-runner.sh`, `deploy/installer.sh` (agent),
`deploy/worker.sh` (worker `claude-subscription`).

Notre instance : box `box-agents` (DEV1-S, IP privée `10.0.0.2`), backend `mcp.example.com`. Ce qui est propre
à notre parc (IP, supervision, inventaire) vit dans `<dépôt-infra>`, `scripts/box-agents/`.

## Feuille de route (décidée par Alexis, 25/09/2026)

Le service tournera **chez client, après la bascule** sur son Scaleway ; box-agents reste d'ici là notre banc de
test (vous trois seulement). Les quatre évolutions sont livrées (25/09/2026) ; **client fait les tests**, et
Julien et un collègue ont été prévenus du **risque de suspension** des comptes Claude (clients et eux-mêmes).

1. ~~Secrets hors des scripts~~ : lus au démarrage dans le Secret Manager (`deploy/secrets.sh`).
2. ~~Historique en direct~~ : chaque message du CLI apposé au fil du run (`run_messages`) à son arrivée (oto-runner#26).
3. ~~Plafond de consommation~~ (backend v1.369.0, dashboard v1.106.0) : jamais au-delà de x % de l'usage **total** du compte (fenêtres 5 h et 7 j,
   usage perso compris). x réglé par l'org, chacun peut descendre plus bas pour son compte (le plus strict
   gagne). Au seuil, le run en cours **finit**, les suivants **attendent** la réinitialisation.
4. ~~Pool d'org~~ (backend v1.370.0, dashboard v1.107.0) : mode perso ou pool réglé **par org** ; prêt de son abonnement **opt-in explicite** par
   membre ; en mode pool, les flottes peuvent y tourner. Box agrandie plus tard, sur mesure.

## Ce qui manque pour un produit

- L'agent derrière le backend (`#1043` : `upsert_sandbox`, `marquer_statut`, pool `claude_subscription`)
  et l'écran dans le dashboard.
- Une sonde `claude auth status` périodique → `needs_login`.
- Maîtriser la synchro des plugins du compte claude.ai dans le sandbox (scope `user:plugins`).
- **Conformité** : les conditions d'Anthropic couvrent l'utilisateur qui se connecte lui-même au binaire
  hébergé, pas clairement des runs ordonnancés d'agents d'org → confirmation écrite d'Anthropic avant
  toute ouverture.
