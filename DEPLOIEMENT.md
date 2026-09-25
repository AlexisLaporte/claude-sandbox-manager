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

1. Un **projet Scaleway dédié** à la box, et dedans deux secrets (chemin `/`) :
   - `ferme-token` : le jeton de l'agent, que vous générez (`openssl rand -hex 24`) — le backend le
     reçoit aussi (§4) ;
   - `oto-worker-secret-abonnement` : le secret machine du worker (§5).
2. Une **application IAM** en lecture seule sur ce seul projet (`SecretManagerReadOnly` +
   `SecretAccess`), et sa clé sur la box dans `/etc/ferme-secrets/scw-key` (600) :
   `SCW_ACCESS_KEY=…`, `SCW_SECRET_KEY=…`, `SCW_PROJECT_ID=…`, et `SCW_REGION=…` si ce n'est pas fr-par.
3. `bash deploy/secrets.sh <hôte-ssh>` pose le chargeur et les drop-ins, puis tire les deux profils
   pour vérifier.

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
