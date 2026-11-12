# Déployer les sandboxes, de A à Z

Ce qu'il faut pour faire tourner des agents oto sur l'abonnement Claude de leurs propriétaires,
sur une box à soi. Quatre pièces : **une box**, **l'agent des sandboxes** (ce dépôt), **un worker
`claude-subscription`** (oto-runner) et **un backend oto** configuré pour joindre l'agent.

```
 navigateur ──(connexion Anthropic)──> claude.com
     │ dashboard /account/claude
     ▼
 backend oto ──réseau privé, jeton──> agent des sandboxes (:8190, root) ──> sandbox-<id> (claude CLI)
     ▲                                          ▲
     └──── file de travaux (API publique) ── worker claude-subscription (même box, non-root)
```

## 1. La box

- **Gabarit** : Ubuntu 24.04. Compter ~1 Go pour le système, l'agent et le worker, plus **500 Mo par run
  simultané** (plafond d'un run). 2 Go → 2 runs ; 8 Go → ~10. Disque ≥ 40 Go : les `~/.claude` des
  sandboxes grossissent (transcripts) — prévoir une purge par âge.
- **Réseau** : la box est sur le **réseau privé** du backend. L'agent n'a **aucune face Internet** : il
  tourne en root et crée des utilisateurs.
- **Groupe de sécurité du fournisseur**, entrée en refus par défaut : 22 (administration), et ce que
  demande votre supervision. Rien d'autre en public.
- **Pare-feu du réseau privé** (le groupe de sécurité ne le filtre souvent pas) et socle minimal :

  ```
  bash deploy/box/box.sh <hôte-ssh> <interface-privée> <ip-privée-du-backend> [<url-de-poussée>]
  ```

  Pose `nftables` (seule l'IP du backend joint le port 8190 sur l'interface privée), les mises à jour
  automatiques, et — si une URL de poussée est donnée (Uptime Kuma « push ») — la sonde de santé de
  l'agent toutes les 2 minutes. À ajouter selon votre socle : CrowdSec, agent de supervision machine,
  SSH par clé seule.

## 2. Les secrets

**Aucun secret sur disque.** Les services lisent les leurs au démarrage dans le Secret Manager Scaleway
(`deploy/secrets/ferme-secrets`, lancé en `ExecStartPre` par les drop-ins `20-secrets.conf`), qui les
écrit dans `/run/ferme-secrets/` (tmpfs, 600). Échec fermé : secret introuvable, le service ne démarre pas.

1. Un **projet Scaleway dédié** à la box, et dedans ses secrets (chemin `/`) :
   - `ferme-token` : le jeton de l'agent, que vous générez (`openssl rand -hex 24`) — le backend le
     reçoit aussi (§4) ;
   - `oto-worker-secret-abonnement` : le secret machine du worker d'abonnement (§5) ;
   - `oto-worker-secret-ferme` : celui du worker `claude-farm` (§5 bis), si vous le posez — un secret
     PROPRE, jamais celui de l'abonnement : révoquer l'un ne coupe pas l'autre ;
   - `oto-worker-secret-anthropic` : celui du worker de la boucle (§5 ter), si vous le posez, propre
     lui aussi.
   - `oto-worker-secret-openrouter` : celui du worker des modèles ouverts (§5 quater), si vous le
     posez, propre lui aussi.
2. Une **application IAM** en lecture seule sur ce seul projet (`SecretManagerReadOnly` +
   `SecretAccess`), et sa clé sur la box dans `/etc/ferme-secrets/scw-key` (600) :
   `SCW_ACCESS_KEY=…`, `SCW_SECRET_KEY=…`, `SCW_PROJECT_ID=…`, et `SCW_REGION=…` si ce n'est pas fr-par.
3. `bash deploy/secrets.sh <hôte-ssh>` pose le chargeur et les drop-ins, puis tire les profils
   (`agent`, `worker`, `worker-farm`, et `worker-anthropic` si son unité est posée) pour vérifier.

Tourner un secret = nouvelle version dans le Secret Manager, puis redémarrer le service. Copie de
référence dans votre coffre (1Password…).

## 3. oto-runner et l'agent

```
bash deploy/oto-runner.sh <hôte-ssh> <checkout d'oto-runner>   # /srv/oto-runner : relais MCP + worker
bash deploy/installer.sh  <hôte-ssh> <ip-privée-de-la-box>      # Node 22, Claude Code épinglé, agent
```

`installer.sh` n'écrit que des valeurs non secrètes dans `/etc/ferme/agent.env` (`FERME_HOST`,
`FERME_RELAIS_PYTHON`), et refuse de continuer s'il y trouve un `FERME_TOKEN`.

Vérifier l'isolement **depuis un vrai run**, après toute modification des brides (`BRIDES` dans
`agent.py`) : poser `tests/isolation.js` dans `/usr/local/share/ferme/`, créer un sandbox de test,
`python3 /opt/ferme/isolation.py <id>`. Attendu : localhost, métadonnées du fournisseur
(169.254.42.42) et réseau privé bloqués ; `/opt` et `/etc/ferme` invisibles ; Anthropic joignable.

## 4. Le backend oto

Dans l'environnement du backend (toutes ses instances) :

```
OTO_FERME_URL=http://<ip-privée-de-la-box>:8190
OTO_FERME_TOKEN=<le secret ferme-token>
```

Contrôle depuis la machine du backend : `curl -H "Authorization: Bearer $OTO_FERME_TOKEN"
http://<ip>:8190/api/sandboxes/test` rend **404** (sandbox inconnu). 401 = mauvais jeton ; délai
dépassé = pare-feu.

## 5. Le worker

Créer son secret machine (admin plateforme) : `oto_admin_runner_worker op=create label="…"` — montré
**une fois** : le poser aussitôt dans le Secret Manager (`oto-worker-secret-abonnement`) et dans le
coffre. Puis :

```
bash deploy/worker.sh <hôte-ssh> <url-du-backend> [<n°>]
```

Le journal doit dire `worker armé — … provider abonnement · modèle sub:sonnet`. Un second worker
(`<n°>` = 2) double les runs simultanés — dans la limite de la mémoire (§1).

## 5 bis. Le worker `claude-farm` (Claude Code sur la clé de l'org)

Il sert la famille `anthropic` en faisant tourner chaque travail en Claude Code dans le sandbox de
l'org du travail, payé par la clé API **que l'org a déposée** : il ne tient aucune clé à lui
(`OTO_RUNNER_ORG_KEYS_ONLY=1`, exigé — sinon refus au démarrage), la plateforme ne paie aucun run.

1. Secret machine **propre** : `oto_admin_runner_worker op=create label="ferme claude-farm"` — montré
   une fois ; le poser aussitôt dans le Secret Manager (`oto-worker-secret-ferme`) et dans le coffre.
   Un worker n'a pas d'org : ce qu'il sert se borne par `OTO_RUNNER_ORGS` (étape 3).
2. `bash deploy/secrets.sh <hôte-ssh>` (pose le drop-in `oto-runner-farm@.service.d/20-secrets.conf`
   et vérifie le profil `worker-farm`), puis l'unité :
   `scp deploy/oto-runner-farm.service <hôte>:/etc/systemd/system/oto-runner-farm@.service`.
3. `/etc/oto-runner/farm.env` (0600, `oto-runner`) sur le modèle de `deploy/farm.env.exemple`, avec
   `OTO_RUNNER_ORGS` = les orgs à essayer (séparées par des virgules). Absent = toutes : ne l'omettre
   qu'une fois le moteur ouvert au parc.
4. `bash deploy/oto-runner.sh <hôte-ssh> <checkout d'oto-runner>` si le code a bougé (un seul venv :
   les deux workers sont relancés), puis `systemctl daemon-reload && systemctl enable --now
   oto-runner-farm@1`.

Le journal doit dire `worker armé — … provider ferme`. Les places de la box (`FERME_RUNS_MAX`)
sont partagées avec l'abonnement.

⚠️ `OTO_RUNNER_ORGS` **restreint** ce worker, il ne détourne rien : les autres workers de la famille
`anthropic` (ceux de la plateforme) servent toujours les mêmes orgs. Un travail va au premier qui le
réserve.

## 5 ter. Le worker `anthropic` (la boucle, sur la clé de l'org ou la clé partagée mesurée)

⚠️ **Sécurité, avant la pose.** Ce worker reçoit la clé PARTAGÉE de la plateforme, servie par le
backend à chaque réservation : quiconque tient la box et son secret `otow_` peut la réclamer. Sur
cette box tournent aussi les sandboxes des orgs. À défaut d'une box sans sandbox client (oto-platform),
poser dans cet ordre : (1) l'`enforce` de `messages:*` et le plancher de réservation actifs sur le
backend (`OTO_CREDITS_MODE_FUNCTIONS`, `OTO_CREDITS_JOB_FLOOR`), à vérifier AVANT ; (2) le worker
restreint à une org d'essai (4ᵉ argument de `worker-anthropic.sh`) ; (3) aucun octroi de la clé hors
achat ; (4) une clé dédiée aux agents, dans son propre workspace Anthropic, si possible.

⚠️ **La mesure repose sur la loyauté du worker.** C'est lui qui réserve chaque appel avant de
l'envoyer et le règle après ; le backend ne voit pas l'appel lui-même. Qui est root sur la box peut
extraire la clé partagée, ou appeler le modèle sans réserver. C'est accepté ici parce que le
propriétaire de la box est aussi celui de la clé partagée. Si ce n'est plus le cas : une clé dédiée,
dans son propre workspace Anthropic, avec un plafond de dépense, et un rapprochement régulier du
relevé du fournisseur avec ce que le budget a réglé (`oto_admin_model_spend`).

Il sert la famille `anthropic` **sans sandbox** : la boucle maison d'oto-runner (`agent_llm`), la
même que sur les workers de la plateforme. Il ne tient aucune clé de modèle
(`OTO_RUNNER_ORG_KEYS_ONLY=1`, `ANTHROPIC_API_KEY` vide) et **promet la mesure**
(`OTO_RUNNER_METERS_USAGE=1`) : il est donc le seul de la box à qui le backend peut servir la clé
**partagée** de l'instance, réservée puis réglée tour par tour au budget de l'org. Les workers
`claude-farm` ne la reçoivent jamais (`RAISON_FERME` : le travail est arrêté), et un worker qui ne
mesure pas rend ces travaux à la file.

Il tourne sous un **utilisateur dynamique** (`DynamicUser=yes`), jamais `oto-runner` : sous le même
uid, il lirait l'environnement des workers de la ferme et de l'abonnement (`/proc/<pid>/environ`),
donc `OTO_FERME_TOKEN` et leurs secrets. Son env vit dans `/etc/oto-runner-anthropic/` (root 700),
**un fichier par instance** (`anthropic-<n°>.env`) : poser une instance ne change jamais ce que sert
une autre. Il ne prend aucune place de la ferme.

Sortie réseau (unité, `IPAddressDeny` / `IPAddressAllow`) : la même liste que les brides des
sandboxes. Ni localhost (sauf le résolveur 127.0.0.53/54), ni le réseau privé (10/8, 172.16/12,
192.168/16, 100.64/10, fc00::/7), ni les métadonnées (169.254/16, fe80::/10 ; fd00:42::42 est
dans fc00::/7), en IPv4 comme en IPv6. Le backend se joint donc par son adresse PUBLIQUE :
`<url-du-backend>` doit se résoudre en une adresse publique sur la box.

⚠️ **`OTO_RUNNER_ORGS` n'est pas une garde du serveur.** C'est un filtre que le worker déclare à
chaque réservation (`org_ids`) ; son secret `otow_` n'est lié à aucune org, et un worker qui omet le
filtre prend toutes les orgs. Il borne une erreur de pose, pas un abus. La frontière est ailleurs :
l'octroi `platform_key:anthropic` (aux seules orgs qui ont acheté) et le mode `enforce` de
`messages:*` sur le backend.

Ce qu'il prend (`db/runner_jobs._servi_par_ce_worker`, oto-backend) : toute la famille `anthropic`
SAUF celle d'une org routée vers la ferme (option `claude_farm`). ⚠️ Une org qui a déposé sa clé et
n'est PAS routée voit donc ses travaux partagés entre la ferme (Claude Code) et la boucle — le
premier qui réserve gagne. Pour la garder en Claude Code, la router vers la ferme ; cela ne vaut
que pour une org À CLÉ PROPRE : sans clé, la ferme arrête ses travaux (`RAISON_FERME`).

1. Secret machine **propre** : `oto_admin_runner_worker op=create label="ferme anthropic (boucle)"` —
   montré une fois ; le poser aussitôt dans le Secret Manager (`oto-worker-secret-anthropic`) et dans
   le coffre.
2. `bash deploy/secrets.sh <hôte-ssh>` (pose le profil `worker-anthropic` et son drop-in).
3. `bash deploy/worker-anthropic.sh <hôte-ssh> <url-du-backend> <n°> <orgs>`, `<orgs>` OBLIGATOIRE :
   les orgs d'essai (`12,34`), ou `toutes` explicitement. Il vérifie le drop-in, le secret et
   oto-runner AVANT de rien poser, et refuse tant que l'ancien fichier commun
   `/etc/oto-runner-anthropic/anthropic.env` existe (il ne le migre pas : le retirer à la main, puis
   reposer chaque instance avec ses orgs). Il écrit ensuite `/etc/oto-runner-anthropic/anthropic-<n°>.env`
   (modèle : `deploy/anthropic.env.exemple`), active et démarre l'instance, et attend 60 s au plus la
   ligne de démarrage de CE démarrage.

La ligne de démarrage doit dire `worker armé — … provider llm …` (le nom du module, `agent_llm`), et
sa clé :
- oto-runner ≥ `88158fe` : `clé SERVIE uniquement (anthropic) : celle de l'org, ou la clé partagée
  réglée à son budget — aucune clé d'environnement` ;
- avant `88158fe` : `clé de l'org UNIQUEMENT (anthropic) — aucune clé de plateforme`. Même
  comportement (« Aucun comportement ne change », `88158fe`) : seul le libellé ignorait la mesure.
  La mesure se vérifie alors par l'env (`OTO_RUNNER_METERS_USAGE=1`) et par le premier travail sur
  clé partagée, servi au lieu d'être rendu à la file.

Toute autre formulation (une clé d'environnement, un autre provider) : `systemctl stop
oto-runner-anthropic@<n°>`. Ce worker exige le module `anthropic` dans `/srv/oto-runner/.venv`
(extra `[anthropic]` d'oto-runner, posé par `deploy/oto-runner.sh`). À vérifier AVANT la première
pose : `/srv/oto-runner/.venv/bin/python -I -c "import anthropic"`. S'il manque, repasser
`deploy/oto-runner.sh` une fois, jamais un `pip install` à la main (le prochain passage du script
l'ignorerait) ; cela relance tous les workers de la box, avec la patience de la ferme (jusqu'à
61 min) : à placer hors des heures de travaux.

Retour arrière : `systemctl disable --now oto-runner-anthropic@<n°>` (arrêt gracieux). Sans worker
qui mesure, aucun travail ne tourne plus sur la clé partagée : ils retournent à la file, rien n'est
perdu. Ce qui reste sur la box est inerte : l'unité et son drop-in (`/etc/systemd/system/
oto-runner-anthropic@.service*`), l'env (`/etc/oto-runner-anthropic/anthropic-<n°>.env`, sans
secret) et le dossier d'état. Le secret machine, lui, reste VALIDE : le révoquer sur l'instance (`oto_admin_runner_worker`)
si le worker ne doit pas revenir, puis supprimer sa version dans le Secret Manager.

## 5 quater. Le worker `openrouter` (la boucle, modèles ouverts via OpenRouter)

Le même worker que §5 ter — boucle maison, sans sandbox, utilisateur dynamique, aucune clé à lui
(`OTO_RUNNER_ORG_KEYS_ONLY=1`, `OTO_RUNNER_OPENAI_API_KEY` vide), mesure promise
(`OTO_RUNNER_METERS_USAGE=1`), même sortie réseau — mais sur la voie Chat Completions
(`OTO_RUNNER_PROVIDER=openai`) vers `OTO_RUNNER_OPENAI_BASE=https://openrouter.ai/api/v1`. C'est
l'HÔTE qui nomme le dépôt de clé `openrouter` (oto-runner `agent_llm_openai._DEPOTS_PAR_HOTE`), donc la
famille que le worker réserve : les modèles ouverts du catalogue (`runner_models`, famille
`openrouter`). Les avertissements de sécurité et de loyauté du §5 ter valent à l'identique pour la
clé OpenRouter partagée : une clé DÉDIÉE aux agents, avec sa propre limite de dépense chez OpenRouter.

Chaque tour épingle les hôtes que le catalogue déclare pour son modèle (`provider.only`) et refuse
tout hôte qui garde ou réutilise les données (`data_collection: deny`) ; le prix réglé est celui du
plus cher de ces hôtes.

Prérequis : un backend qui connaît la famille `openrouter` (catalogue, dépôt de clé, règlement
`openrouter.messages:<model>`) et un oto-runner qui reconnaît l'hôte. Aucun module de plus dans le
venv (`requests` seul).

1. Secret machine **propre** : `oto_admin_runner_worker op=create label="ferme openrouter (boucle)"`
   — montré une fois ; le poser aussitôt dans le Secret Manager (`oto-worker-secret-openrouter`).
2. `bash deploy/secrets.sh <hôte-ssh>` (pose le profil `worker-openrouter` et son drop-in).
3. `bash deploy/worker-openrouter.sh <hôte-ssh> <url-du-backend> <n°> <orgs>` — mêmes vérifications
   et même attente que `worker-anthropic.sh` ; env dans
   `/etc/oto-runner-openrouter/openrouter-<n°>.env` (modèle : `deploy/openrouter.env.exemple`).

La ligne de démarrage doit dire `worker armé — … provider openai …` (le module
`agent_llm_openai`) et `clé SERVIE uniquement (openrouter) : celle de l'org, ou la clé partagée
réglée à son budget — aucune clé d'environnement` (oto-runner `worker._dire_la_cle`). Un dépôt
autre qu'`openrouter` dans cette ligne veut dire que l'hôte n'est pas reconnu (oto-runner trop
ancien) : `systemctl stop oto-runner-openrouter@<n°>`.

Retour arrière : `systemctl disable --now oto-runner-openrouter@<n°>`, comme au §5 ter.

## 6. Ouvrir à des personnes

Le chemin est fermé par défaut. Un admin l'ouvre compte par compte :
`oto_admin_set_option(entity_type=user, entity_id=<sub>, option="claude_subscription", on=true)`.
Une personne a parfois plusieurs comptes (un par émetteur d'identité) : les ouvrir tous.

La personne se connecte sur le dashboard (`/account/claude`), en choisissant chez Anthropic **son**
compte (une organisation Team ou Enterprise paierait à sa place), puis pose un agent sur un modèle
`sub:*`.

## 7. Recette

Une procédure qui appelle `oto_whoami`, un agent programmé sur `sub:sonnet` toutes les 5 minutes, un
run : le journal du worker dit `done … modèle servi claude-sonnet-…`, et
`/var/lib/oto-runner-abonnement/<n°>/passages/` garde la trace (réponse, outils, forfait remonté).
Supprimer l'agent de test ensuite.

## Ce qui n'est pas automatisé

- Pas de CI : les scripts se lancent depuis un poste qui a la clé SSH de la box.
- La box n'est pas sauvegardée : la perdre = chaque personne se reconnecte.
