#!/usr/bin/env bash
# Pose et démarre le worker `claude-subscription` sur la box (après oto-runner.sh, secrets.sh, installer.sh).
#   bash deploy/worker.sh <hôte-ssh> <url-du-backend> [<n° d'instance, défaut 1>]
# Aucun secret ici : OTO_WORKER_SECRET et OTO_FERME_TOKEN viennent du Secret Manager au démarrage
# (deploy/secrets.sh, profil `worker`). /etc/oto-runner/abonnement.env ne porte que le reste.
set -euo pipefail
HOTE_SSH=$1 BACKEND=${2%/} N=${3:-1}
cd "$(dirname "$0")"
scp -q oto-runner-abonnement.service "$HOTE_SSH":/etc/systemd/system/oto-runner-abonnement@.service
ssh "$HOTE_SSH" BACKEND="'$BACKEND'" N="'$N'" bash -s <<'DISTANT'
set -euo pipefail
: "${BACKEND:?}" "${N:?}"
[ -x /usr/local/sbin/ferme-secrets ] || { echo "chargeur de secrets absent : lance d'abord deploy/secrets.sh" >&2; exit 1; }
. /etc/ferme/agent.env
id oto-runner >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin oto-runner
install -d -m 750 -o oto-runner -g oto-runner /etc/oto-runner
umask 077
# Écrit à côté puis renommé : un échec en cours de route ne laisse jamais un fichier tronqué.
cat > /etc/oto-runner/abonnement.env.neuf <<FIN
OTO_RUNNER_PROVIDER=claude-subscription
OTO_RUNNER_ARMED=1
OTO_BASE=$BACKEND
OTO_MCP_URL=$BACKEND/mcp
OTO_FERME_URL=http://$FERME_HOST:8190
FIN
chown oto-runner:oto-runner /etc/oto-runner/abonnement.env.neuf
mv /etc/oto-runner/abonnement.env.neuf /etc/oto-runner/abonnement.env
systemctl daemon-reload
systemctl enable oto-runner-abonnement@$N >/dev/null
systemctl restart oto-runner-abonnement@$N
sleep 5
systemctl is-active oto-runner-abonnement@$N
journalctl -u oto-runner-abonnement@$N -n 1 --no-pager -o cat | cut -c1-160
DISTANT
