#!/usr/bin/env bash
# Pose et démarre le worker `anthropic` (la boucle) sur la box (après oto-runner.sh et secrets.sh).
#   bash deploy/worker-anthropic.sh <hôte-ssh> <url-du-backend> [<n° d'instance, défaut 1>]
# Aucun secret ici : OTO_WORKER_SECRET vient du Secret Manager au démarrage (profil
# `worker-anthropic`). /etc/oto-runner/anthropic.env ne porte que le reste.
set -euo pipefail
HOTE_SSH=$1 BACKEND=${2%/} N=${3:-1}
cd "$(dirname "$0")"
scp -q oto-runner-anthropic.service "$HOTE_SSH":/etc/systemd/system/oto-runner-anthropic@.service
ssh "$HOTE_SSH" BACKEND="'$BACKEND'" N="'$N'" bash -s <<'DISTANT'
set -euo pipefail
: "${BACKEND:?}" "${N:?}"
[ -f "/etc/systemd/system/oto-runner-anthropic@.service.d/20-secrets.conf" ] || {
  echo "drop-in de secrets absent : lance d'abord deploy/secrets.sh" >&2; exit 1; }
/usr/local/sbin/ferme-secrets worker-anthropic >/dev/null || {
  echo "profil worker-anthropic illisible (secret oto-worker-secret-anthropic posé ?)" >&2; exit 1; }
id oto-runner >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin oto-runner
install -d -m 750 -o oto-runner -g oto-runner /etc/oto-runner
umask 077
# Écrit à côté puis renommé : un échec en cours de route ne laisse jamais un fichier tronqué.
cat > /etc/oto-runner/anthropic.env.neuf <<FIN
OTO_RUNNER_PROVIDER=anthropic
OTO_RUNNER_ORG_KEYS_ONLY=1
OTO_RUNNER_METERS_USAGE=1
OTO_RUNNER_ARMED=1
OTO_BASE=$BACKEND
OTO_MCP_URL=$BACKEND/mcp
ANTHROPIC_API_KEY=
FIN
chown oto-runner:oto-runner /etc/oto-runner/anthropic.env.neuf
mv /etc/oto-runner/anthropic.env.neuf /etc/oto-runner/anthropic.env
systemctl daemon-reload
systemctl enable oto-runner-anthropic@$N >/dev/null
systemctl restart oto-runner-anthropic@$N
sleep 5
systemctl is-active oto-runner-anthropic@$N
# La ligne de démarrage seule (« worker armé — … ») : jamais la ligne de commande de l'unité.
journalctl -u oto-runner-anthropic@$N -n 20 --no-pager -o cat | grep -m1 "worker armé" | cut -c1-200
DISTANT
