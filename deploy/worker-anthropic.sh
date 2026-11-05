#!/usr/bin/env bash
# Pose et démarre UNE instance du worker `anthropic` (la boucle) sur la box (après oto-runner.sh et
# secrets.sh).
#   bash deploy/worker-anthropic.sh <hôte-ssh> <url-du-backend> <n° d'instance> <orgs : 12,34 | toutes>
# Les orgs sont OBLIGATOIRES : `toutes` se dit explicitement. Elles ne valent que pour l'instance
# posée, dont le fichier d'env est PROPRE (/etc/oto-runner-anthropic/anthropic-<n°>.env) : poser
# une instance ne change jamais ce que sert une autre.
# Aucun secret ici : OTO_WORKER_SECRET vient du Secret Manager au démarrage (profil
# `worker-anthropic`). Le fichier d'env (root, 600) ne porte que le reste.
set -euo pipefail
[ $# -eq 4 ] || {
  echo "usage : $0 <hôte-ssh> <url-du-backend> <n° d'instance> <orgs : 12,34 | toutes>" >&2; exit 2; }
HOTE_SSH=$1 BACKEND=${2%/} N=$3 ORGS=$4
# Ces valeurs sont écrites dans un fichier d'env et dans des commandes distantes : une forme
# stricte, sinon un saut de ligne ajouterait des variables (ex. ANTHROPIC_API_KEY=…).
[[ $BACKEND =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || { echo "url du backend refusée : https://hôte[:port] attendu" >&2; exit 2; }
[[ $ORGS == toutes || $ORGS =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo "orgs refusées : des entiers séparés par des virgules, ou « toutes »" >&2; exit 2; }
[[ $N =~ ^[0-9]{1,3}$ ]] || { echo "n° d'instance refusé : un entier attendu" >&2; exit 2; }
cd "$(dirname "$0")"
# Les vérifications D'ABORD : rien n'est posé sur une box qui ne pourrait pas le démarrer.
ssh "$HOTE_SSH" bash -s <<'VERIF'
set -euo pipefail
# L'ancien fichier COMMUN à toutes les instances : il n'est plus lu, et le migrer en silence
# changerait ce que sert chaque instance. Il se retire à la main, en connaissance de cause.
[ ! -e /etc/oto-runner-anthropic/anthropic.env ] || {
  echo "/etc/oto-runner-anthropic/anthropic.env existe : c'est l'ancien fichier commun à toutes les" \
       "instances, qui n'est plus lu. Note ses OTO_RUNNER_ORGS, arrête les instances" \
       "(systemctl stop 'oto-runner-anthropic@*'), supprime-le, puis repose chaque instance avec" \
       "ses orgs." >&2; exit 1; }
[ -f "/etc/systemd/system/oto-runner-anthropic@.service.d/20-secrets.conf" ] || {
  echo "drop-in de secrets absent : lance d'abord deploy/secrets.sh" >&2; exit 1; }
/usr/local/sbin/ferme-secrets worker-anthropic >/dev/null || {
  echo "profil worker-anthropic illisible (secret oto-worker-secret-anthropic posé ?)" >&2; exit 1; }
[ -x /srv/oto-runner/.venv/bin/python ] || { echo "oto-runner absent : deploy/oto-runner.sh" >&2; exit 1; }
VERIF
scp -q oto-runner-anthropic.service "$HOTE_SSH":/etc/systemd/system/oto-runner-anthropic@.service
ssh "$HOTE_SSH" BACKEND="'$BACKEND'" N="'$N'" ORGS="'$ORGS'" bash -s <<'DISTANT'
set -euo pipefail
: "${BACKEND:?}" "${N:?}" "${ORGS:?}"
UNITE="oto-runner-anthropic@$N"
ENV="/etc/oto-runner-anthropic/anthropic-$N.env"
# Son propre dossier, root 700 : /etc/oto-runner appartient aux workers `oto-runner`.
install -d -m 700 -o root -g root /etc/oto-runner-anthropic
umask 077
# Écrit à côté puis renommé : un échec en cours de route ne laisse jamais un fichier tronqué.
# root 600 : seul systemd le lit (l'unité tourne sous un utilisateur dynamique).
cat > "$ENV.neuf" <<FIN
OTO_RUNNER_PROVIDER=anthropic
OTO_RUNNER_ORG_KEYS_ONLY=1
OTO_RUNNER_METERS_USAGE=1
OTO_RUNNER_ARMED=1
OTO_BASE=$BACKEND
OTO_MCP_URL=$BACKEND/mcp
ANTHROPIC_API_KEY=
FIN
# Les orgs de CETTE instance ; `toutes` = pas de ligne. Une restriction que le worker déclare,
# pas une garde du serveur (DEPLOIEMENT.md §5 ter).
[ "$ORGS" = toutes ] || echo "OTO_RUNNER_ORGS=$ORGS" >> "$ENV.neuf"
chown root:root "$ENV.neuf"
mv "$ENV.neuf" "$ENV"
systemctl daemon-reload
systemctl enable "$UNITE" >/dev/null
systemctl restart "$UNITE"
# La ligne de démarrage de CE démarrage (son InvocationID), attendue 60 s au plus. La ligne seule
# (« worker armé — … ») : jamais la ligne de commande de l'unité.
ID=$(systemctl show -p InvocationID --value "$UNITE")
LIGNE=
for _ in $(seq 30); do
  LIGNE=$(journalctl _SYSTEMD_INVOCATION_ID="$ID" --no-pager -o cat 2>/dev/null \
            | grep -m1 -o "worker armé.\{0,200\}" || true)
  [ -z "$LIGNE" ] || break
  systemctl is-active --quiet "$UNITE" || {
    echo "$UNITE n'est pas actif : voir journalctl -u $UNITE" >&2; exit 1; }
  sleep 2
done
[ -n "$LIGNE" ] || { echo "$UNITE : aucune ligne « worker armé » en 60 s, voir journalctl -u $UNITE" >&2; exit 1; }
echo "$UNITE actif, orgs : $ORGS"
echo "$LIGNE"
DISTANT
