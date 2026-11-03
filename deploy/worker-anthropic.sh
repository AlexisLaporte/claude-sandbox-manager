#!/usr/bin/env bash
# Pose et démarre le worker `anthropic` (la boucle) sur la box (après oto-runner.sh et secrets.sh).
#   bash deploy/worker-anthropic.sh <hôte-ssh> <url-du-backend> [<n° d'instance, défaut 1> [<orgs : 12,34>]]
# Aucun secret ici : OTO_WORKER_SECRET vient du Secret Manager au démarrage (profil
# `worker-anthropic`). /etc/oto-runner-anthropic/anthropic.env (root, 600) ne porte que le reste.
set -euo pipefail
HOTE_SSH=$1 BACKEND=${2%/} N=${3:-1} ORGS=${4:-}
# Ces deux valeurs sont écrites dans un fichier d'env et dans des commandes distantes : une
# forme stricte, sinon un saut de ligne ajouterait des variables (ex. ANTHROPIC_API_KEY=…).
[[ $BACKEND =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || { echo "url du backend refusée : https://hôte[:port] attendu" >&2; exit 2; }
[[ -z $ORGS || $ORGS =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo "orgs refusées : des entiers séparés par des virgules" >&2; exit 2; }
[[ $N =~ ^[0-9]{1,3}$ ]] || { echo "n° d'instance refusé : un entier attendu" >&2; exit 2; }
cd "$(dirname "$0")"
# Les vérifications D'ABORD : rien n'est posé sur une box qui ne pourrait pas le démarrer.
ssh "$HOTE_SSH" bash -s <<'VERIF'
set -euo pipefail
[ -f "/etc/systemd/system/oto-runner-anthropic@.service.d/20-secrets.conf" ] || {
  echo "drop-in de secrets absent : lance d'abord deploy/secrets.sh" >&2; exit 1; }
/usr/local/sbin/ferme-secrets worker-anthropic >/dev/null || {
  echo "profil worker-anthropic illisible (secret oto-worker-secret-anthropic posé ?)" >&2; exit 1; }
[ -x /srv/oto-runner/.venv/bin/python ] || { echo "oto-runner absent : deploy/oto-runner.sh" >&2; exit 1; }
VERIF
scp -q oto-runner-anthropic.service "$HOTE_SSH":/etc/systemd/system/oto-runner-anthropic@.service
ssh "$HOTE_SSH" BACKEND="'$BACKEND'" N="'$N'" ORGS="'$ORGS'" bash -s <<'DISTANT'
set -euo pipefail
: "${BACKEND:?}" "${N:?}"
# Son propre dossier, root 700 : /etc/oto-runner appartient aux workers `oto-runner`.
install -d -m 700 -o root -g root /etc/oto-runner-anthropic
umask 077
# Écrit à côté puis renommé : un échec en cours de route ne laisse jamais un fichier tronqué.
# root 600 : seul systemd le lit (l'unité tourne sous un utilisateur dynamique).
cat > /etc/oto-runner-anthropic/anthropic.env.neuf <<FIN
OTO_RUNNER_PROVIDER=anthropic
OTO_RUNNER_ORG_KEYS_ONLY=1
OTO_RUNNER_METERS_USAGE=1
OTO_RUNNER_ARMED=1
OTO_BASE=$BACKEND
OTO_MCP_URL=$BACKEND/mcp
ANTHROPIC_API_KEY=
FIN
# Pour essayer sur une ou deux orgs d'abord : ce worker ne réserve que celles-là.
[ -z "$ORGS" ] || echo "OTO_RUNNER_ORGS=$ORGS" >> /etc/oto-runner-anthropic/anthropic.env.neuf
chown root:root /etc/oto-runner-anthropic/anthropic.env.neuf
mv /etc/oto-runner-anthropic/anthropic.env.neuf /etc/oto-runner-anthropic/anthropic.env
systemctl daemon-reload
systemctl enable oto-runner-anthropic@$N >/dev/null
systemctl restart oto-runner-anthropic@$N
sleep 5
systemctl is-active oto-runner-anthropic@$N
# La ligne de démarrage seule (« worker armé — … ») : jamais la ligne de commande de l'unité.
journalctl -u oto-runner-anthropic@$N -n 20 --no-pager -o cat | grep -m1 -o "worker armé.\{0,200\}"
DISTANT
