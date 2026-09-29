#!/usr/bin/env bash
# Pose oto-runner sur la box de la ferme : bash deploy/oto-runner.sh <hôte-ssh> <checkout oto-runner>
# Deux usages d'un même code : le relais MCP lancé DANS les sandboxes (d'où /srv, lisible par eux —
# /opt leur est invisible) et les workers `claude-subscription` et `claude-farm`, sous l'utilisateur
# `oto-runner` — un seul venv : le mettre à jour relance les deux.
set -euo pipefail
HOTE_SSH=$1 CHECKOUT=$2
ssh "$HOTE_SSH" mkdir -p /srv/oto-runner/src
rsync -a --delete --exclude .venv --exclude .git --exclude '__pycache__' --exclude '*.log' \
  --exclude '*.yaml' --exclude '*.json' --exclude tests "$CHECKOUT"/ "$HOTE_SSH":/srv/oto-runner/src/
ssh "$HOTE_SSH" bash -s <<'DISTANT'
set -euo pipefail
command -v python3 >/dev/null && dpkg -s python3-venv >/dev/null 2>&1 || apt-get install -y -qq python3-venv >/dev/null
id oto-runner >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin oto-runner
[ -x /srv/oto-runner/.venv/bin/python ] || python3 -m venv /srv/oto-runner/.venv
/srv/oto-runner/.venv/bin/pip install -q --upgrade /srv/oto-runner/src
chown -R root:root /srv/oto-runner && chmod -R a+rX /srv/oto-runner
/srv/oto-runner/.venv/bin/python -c "import oto_runner.relais_mcp, oto_runner.agent_abonnement; print('oto-runner posé')"
# Un worker déjà lancé garde l'ancien code en mémoire : on le relance (arrêt gracieux, cf. l'unité).
systemctl try-restart 'oto-runner-abonnement@*' 'oto-runner-farm@*' && echo "workers relancés"
DISTANT
