#!/usr/bin/env bash
# Prépare une box Ubuntu 24.04 NEUVE pour les sandboxes (avant deploy/installer.sh).
#   bash deploy/box/box.sh <hôte-ssh> <interface-privée> <ip-du-backend> [<url-de-poussée-sonde>]
# Idempotent. Ne touche ni au Security Group du fournisseur, ni à SSH : voir DEPLOIEMENT.md.
set -euo pipefail
HOTE_SSH=$1 IFACE=$2 IP_BACKEND=$3 PUSH_URL=${4:-}
PORT=8190
cd "$(dirname "$0")"
sed -e "s/@IFACE_PRIVEE@/$IFACE/" -e "s/@IP_BACKEND@/$IP_BACKEND/" -e "s/@PORT@/$PORT/" \
  nftables.conf.modele | ssh "$HOTE_SSH" 'cat > /etc/nftables.conf'
ssh "$HOTE_SSH" bash -s <<'DISTANT'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq nftables unattended-upgrades curl >/dev/null
systemctl enable --now nftables >/dev/null
nft -f /etc/nftables.conf
nft list table inet sandbox_pn >/dev/null && echo "pare-feu du réseau privé posé"
systemctl enable --now unattended-upgrades >/dev/null
DISTANT
if [ -n "$PUSH_URL" ]; then
  scp -q sonde/sandbox-sonde "$HOTE_SSH":/usr/local/bin/sandbox-sonde
  scp -q sonde/sandbox-sonde.service sonde/sandbox-sonde.timer "$HOTE_SSH":/etc/systemd/system/
  IP_PRIVEE=$(ssh "$HOTE_SSH" "ip -4 -br addr show $IFACE | awk '{print \$3}' | cut -d/ -f1")
  ssh "$HOTE_SSH" "chmod 755 /usr/local/bin/sandbox-sonde && install -d -m 700 /etc/sandbox-sonde && \
    umask 077 && printf 'AGENT_URL=http://%s:$PORT\nPUSH_URL=%s\n' '$IP_PRIVEE' '$PUSH_URL' > /etc/sandbox-sonde/env && \
    systemctl daemon-reload && systemctl enable --now sandbox-sonde.timer >/dev/null && echo 'sonde posée'"
fi
