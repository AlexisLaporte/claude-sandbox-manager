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

## 2. oto-runner et l'agent

```
bash deploy/oto-runner.sh <hôte-ssh> <checkout d'oto-runner>   # /srv/oto-runner : relais MCP + worker
bash deploy/installer.sh  <hôte-ssh> <ip-privée-de-la-box>      # Node 22, Claude Code épinglé, agent
```

`installer.sh` crée **une seule fois** `/etc/ferme/agent.env` (0600) : `FERME_TOKEN` (le jeton de
l'agent, généré) et `FERME_HOST`. **Copiez `FERME_TOKEN` dans votre coffre de secrets** : le backend et
le worker en ont besoin. Le changer = mettre à jour le coffre, le backend et le worker.

Vérifier l'isolement **depuis un vrai run**, après toute modification des brides (`BRIDES` dans
`agent.py`) : poser `tests/isolation.js` dans `/usr/local/share/ferme/`, créer un sandbox de test,
`python3 /opt/ferme/isolation.py <id>`. Attendu : localhost, métadonnées du fournisseur
(169.254.42.42) et réseau privé bloqués ; `/opt` et `/etc/ferme` invisibles ; Anthropic joignable.

## 3. Le backend oto

Dans l'environnement du backend (toutes ses instances) :

```
OTO_FERME_URL=http://<ip-privée-de-la-box>:8190
OTO_FERME_TOKEN=<FERME_TOKEN>
```

Contrôle depuis la machine du backend : `curl -H "Authorization: Bearer $OTO_FERME_TOKEN"
http://<ip>:8190/api/sandboxes/test` rend **404** (sandbox inconnu). 401 = mauvais jeton ; délai
dépassé = pare-feu.

## 4. Le worker

Créer son secret machine (admin plateforme) : `oto_admin_runner_worker op=create label="…"` — montré
**une fois**, à ranger aussitôt dans le coffre. Puis, le secret sur l'entrée standard :

```
<commande qui lit le secret dans le coffre> | bash deploy/worker.sh <hôte-ssh> <url-du-backend> [<n°>]
```

Le journal doit dire `worker armé — … provider abonnement · modèle sub:sonnet`. Un second worker
(`<n°>` = 2) double les runs simultanés — dans la limite de la mémoire (§1).

## 5. Ouvrir à des personnes

Le chemin est fermé par défaut. Un admin l'ouvre compte par compte :
`oto_admin_set_option(entity_type=user, entity_id=<sub>, option="claude_subscription", on=true)`.
Une personne a parfois plusieurs comptes (un par émetteur d'identité) : les ouvrir tous.

La personne se connecte sur le dashboard (`/account/claude`), en choisissant chez Anthropic **son**
compte (une organisation Team ou Enterprise paierait à sa place), puis pose un agent sur un modèle
`sub:*`.

## 6. Recette

Une procédure qui appelle `oto_whoami`, un agent programmé sur `sub:sonnet` toutes les 5 minutes, un
run : le journal du worker dit `done … modèle servi claude-sonnet-…`, et
`/var/lib/oto-runner-abonnement/<n°>/passages/` garde la trace (réponse, outils, forfait remonté).
Supprimer l'agent de test ensuite.

## Ce qui n'est pas automatisé

- Pas de CI : les scripts se lancent depuis un poste qui a la clé SSH de la box.
- Les secrets restent dans des fichiers 0600 sur la box ; les lire depuis le gestionnaire de secrets du
  fournisseur au démarrage est à faire avant une ouverture large.
- La box n'est pas sauvegardée : la perdre = chaque personne se reconnecte.
