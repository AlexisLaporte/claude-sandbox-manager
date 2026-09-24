#!/usr/bin/env bash
# Pose et démarre le worker `claude-subscription` sur la box (après oto-runner.sh et installer.sh).
#   bash deploy/worker.sh <hôte-ssh> <url-du-backend> [<n° d'instance, défaut 1>]
# Le secret machine du worker (`otow_…`, oto_admin_runner_worker op=create) se lit sur l'ENTRÉE
# standard, pour ne jamais passer par la ligne de commande ni l'historique :
#   op read "op://oto/<élément>/credential" | bash deploy/worker.sh box-agents https://mcp.example.com
# Le jeton de l'agent est repris de /etc/ferme/agent.env : un seul jeton, deux consommateurs.
set -euo pipefail
HOTE_SSH=$1 BACKEND=${2%/} N=${3:-1}
SECRET=$(cat)
case "$SECRET" in otow_*) ;; *) echo "secret de worker attendu sur l'entrée standard (otow_…)" >&2; exit 1 ;; esac
cd "$(dirname "$0")"
scp -q oto-runner-abonnement.service "$HOTE_SSH":/etc/systemd/system/oto-runner-abonnement@.service
# Le secret passe en PREMIÈRE ligne de l'entrée standard, le script ensuite : rien sur la ligne de commande.
{ printf '%s\n' "$SECRET"; cat <<'DISTANT'; } | ssh "$HOTE_SSH" "BACKEND='$BACKEND' N='$N' bash -c 'read -r SECRET; export SECRET; exec bash -s'"
set -euo pipefail
: "${BACKEND:?}" "${N:?}" "${SECRET:?}"
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
OTO_FERME_TOKEN=$FERME_TOKEN
OTO_WORKER_SECRET=$SECRET
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
