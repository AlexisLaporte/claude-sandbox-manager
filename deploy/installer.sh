#!/usr/bin/env bash
# Pose l'agent ferme sur une box Ubuntu 24.04 : bash deploy/installer.sh <hôte-ssh> <ip-privée>
set -euo pipefail
HOTE_SSH=$1 IP_PRIVEE=$2
CLAUDE_CODE_VERSION=2.1.281   # épinglée : un changement de version change le comportement des runs
cd "$(dirname "$0")/.."
ssh "$HOTE_SSH" "mkdir -p /opt/ferme /etc/ferme && chmod 700 /etc/ferme"
scp -q agent.py index.html "$HOTE_SSH":/opt/ferme/
scp -q deploy/ferme-agent.service "$HOTE_SSH":/etc/systemd/system/
ssh "$HOTE_SSH" bash -s -- "$CLAUDE_CODE_VERSION" "$IP_PRIVEE" <<'DISTANT'
set -euo pipefail
VERSION=$1 IP=$2
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
[ "$(claude --version 2>/dev/null | cut -d' ' -f1)" = "$VERSION" ] || \
  npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@$VERSION" >/dev/null
# Seulement des valeurs NON secrètes. FERME_TOKEN vient du Secret Manager au démarrage
# (deploy/secrets.sh) : un jeton trouvé sur disque est une fuite, on refuse de continuer.
if [ -f /etc/ferme/agent.env ] && grep -q '^FERME_TOKEN=' /etc/ferme/agent.env; then
  echo "FERME_TOKEN écrit dans /etc/ferme/agent.env : retire-le (il vit dans le Secret Manager)" >&2
  exit 1
fi
umask 077
touch /etc/ferme/agent.env
poser() { grep -q "^$1=" /etc/ferme/agent.env && sed -i "s|^$1=.*|$1=$2|" /etc/ferme/agent.env || echo "$1=$2" >> /etc/ferme/agent.env; }
poser FERME_HOST "$IP"
poser FERME_RELAIS_PYTHON /srv/oto-runner/.venv/bin/python
[ -x /usr/local/sbin/ferme-secrets ] || { echo "chargeur de secrets absent : lance d'abord deploy/secrets.sh" >&2; exit 1; }
systemctl daemon-reload
systemctl enable --now ferme-agent
systemctl restart ferme-agent
sleep 1
systemctl is-active ferme-agent
echo "node $(node --version) · claude $(claude --version)"
DISTANT
