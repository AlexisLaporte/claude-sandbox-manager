#!/usr/bin/env bash
# Pose le chargeur de secrets et ses drop-ins : les services lisent leurs secrets au démarrage dans
# le Secret Manager Scaleway, jamais sur disque.   bash deploy/secrets.sh <hôte-ssh>
# Prérequis (DEPLOIEMENT.md §Secrets) : /etc/ferme-secrets/scw-key (600) sur la box, et dans le
# projet dédié les secrets `ferme-token`, `oto-worker-secret-abonnement` et `oto-worker-secret-ferme`.
set -euo pipefail
HOTE_SSH=$1
cd "$(dirname "$0")/secrets"
ssh "$HOTE_SSH" 'test -s /etc/ferme-secrets/scw-key && grep -q "^SCW_PROJECT_ID=" /etc/ferme-secrets/scw-key' || {
  echo "/etc/ferme-secrets/scw-key absente ou sans SCW_PROJECT_ID : voir DEPLOIEMENT.md §Secrets" >&2; exit 1; }
scp -q ferme-secrets "$HOTE_SSH":/usr/local/sbin/ferme-secrets
ssh "$HOTE_SSH" 'chmod 750 /usr/local/sbin/ferme-secrets && install -d /etc/systemd/system/ferme-agent.service.d "/etc/systemd/system/oto-runner-abonnement@.service.d" "/etc/systemd/system/oto-runner-farm@.service.d"'
scp -q ferme-agent.service.d/20-secrets.conf "$HOTE_SSH":/etc/systemd/system/ferme-agent.service.d/20-secrets.conf
scp -q "oto-runner-abonnement@.service.d/20-secrets.conf" "$HOTE_SSH":"/etc/systemd/system/oto-runner-abonnement@.service.d/20-secrets.conf"
scp -q "oto-runner-farm@.service.d/20-secrets.conf" "$HOTE_SSH":"/etc/systemd/system/oto-runner-farm@.service.d/20-secrets.conf"
ssh "$HOTE_SSH" 'systemctl daemon-reload && /usr/local/sbin/ferme-secrets agent && /usr/local/sbin/ferme-secrets worker && /usr/local/sbin/ferme-secrets worker-farm'
