#!/usr/bin/env bash
# Install or update the ladder API on the server. Idempotent — running it
# twice is the normal way to deploy a new version.
#
#   HOST=root@example.com LADDER_HOST=ladder.example.com ./deploy/deploy.sh
#   HOST=... LADDER_HOST=... PORT=443 ./deploy/deploy.sh   # non-standard ssh port
#
# Only `ladder/`, `server/` and requirements.txt are copied. The client, the
# tests and anything under data/ stay on the developer machine: data/ holds
# real Steam IDs, and there is no reason for them to exist on a public host.
set -euo pipefail

# No defaults for these: a public repository should not carry someone's
# server address, and a wrong guess would deploy to the wrong machine.
: "${HOST:?set HOST, e.g. HOST=deploy@example.com}"
: "${LADDER_HOST:?set LADDER_HOST, e.g. LADDER_HOST=ladder.example.com}"
PORT="${PORT:-22}"
# Whatever the sibling vhosts already use on this host.
SSL_KEY="${SSL_KEY:-/etc/nginx/ssl/privkey.key}"
KEY="${KEY:-$HOME/.ssh/id_ed25519}"
APP_DIR=/opt/forts-ladder
DATA_DIR=/var/lib/forts-ladder
SERVICE=forts-ladder

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ssh_opts=(-p "$PORT" -i "$KEY" -o BatchMode=yes)
# scp spells the port -P; -p means "preserve timestamps" and would swallow the
# number as a filename.
scp_opts=(-P "$PORT" -i "$KEY" -o BatchMode=yes)

echo "==> preparing $HOST"
ssh "${ssh_opts[@]}" "$HOST" bash -euo pipefail <<REMOTE
# Dedicated system account: the API has no business running as root next to
# the other services on this machine.
id -u fortsladder >/dev/null 2>&1 || \
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin fortsladder
mkdir -p "$APP_DIR" "$DATA_DIR" /etc/forts-ladder
chown fortsladder:fortsladder "$DATA_DIR"
chmod 750 "$DATA_DIR" /etc/forts-ladder
apt-get install -y --no-install-recommends python3-venv >/dev/null
REMOTE

echo "==> copying source"
# tar over ssh rather than rsync: this runs from a Windows dev machine where
# rsync is not present, and tar needs nothing installed on either side.
#
# The two package directories are removed first so a file deleted from the repo
# also disappears here. Scoped to exactly those two names — the venv, the unit
# and the database live elsewhere and are never touched.
tar -cz --exclude='__pycache__' --exclude='*.pyc' \
    -C "$here" ladder server requirements.txt \
  | ssh "${ssh_opts[@]}" "$HOST" \
      "rm -rf '$APP_DIR/ladder' '$APP_DIR/server' && tar -xz -C '$APP_DIR'"

echo "==> installing unit and vhost"
scp "${scp_opts[@]}" -q \
    "$here/deploy/forts-ladder.service" "$HOST:/etc/systemd/system/$SERVICE.service"
scp "${scp_opts[@]}" -q \
    "$here/deploy/nginx-ladder.conf" "$HOST:/etc/nginx/sites-available/$LADDER_HOST"

echo "==> host-specific settings"
# The public base URL lives here, not in the unit file in the repository.
ssh "${ssh_opts[@]}" "$HOST"     "touch /etc/forts-ladder/env && chmod 600 /etc/forts-ladder/env &&      grep -q '^LADDER_BASE_URL=' /etc/forts-ladder/env ||      echo 'LADDER_BASE_URL=https://$LADDER_HOST' >> /etc/forts-ladder/env"

echo "==> venv, service, nginx"
ssh "${ssh_opts[@]}" "$HOST" bash -euo pipefail <<REMOTE
cd "$APP_DIR"
test -d .venv || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt uvicorn
chown -R fortsladder:fortsladder "$APP_DIR"

# Substitute the placeholders the repository copy carries.
sed -i -e "s|ladder\.example\.com|$LADDER_HOST|g" -e "s|/etc/nginx/ssl/privkey\.key|$SSL_KEY|" "/etc/nginx/sites-available/$LADDER_HOST"
ln -sfn "../sites-available/$LADDER_HOST" "/etc/nginx/sites-enabled/$LADDER_HOST"

# Validate before reloading: a broken config would take the other vhosts on
# this host down with it.
nginx -t
systemctl reload nginx

systemctl daemon-reload
systemctl enable --now $SERVICE
systemctl restart $SERVICE
sleep 2
systemctl is-active --quiet $SERVICE || { journalctl -u $SERVICE -n 30 --no-pager; exit 1; }
echo "--- health"
curl -fsS http://127.0.0.1:8010/health && echo
REMOTE

echo "==> done"
