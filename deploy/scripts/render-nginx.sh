#!/usr/bin/env bash
# Render the nginx config for your hostname and install it.
#   bash deploy/scripts/render-nginx.sh app.example.com [--no-reload]
# Needs the certificate files (certbot first, DEPLOY.md step 8) and the access key
# (bash deploy/scripts/make-access-key.sh, step 7).
set -euo pipefail

APP_HOST="${1:-}"
[ -n "$APP_HOST" ] || { echo "usage: $0 app.example.com [--no-reload]" >&2; exit 1; }
case "$APP_HOST" in *[!A-Za-z0-9.-]*) echo "invalid hostname" >&2; exit 1;; esac
[ -f /etc/nginx/jev-access.conf ] || { echo "missing /etc/nginx/jev-access.conf: run deploy/scripts/make-access-key.sh first" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../nginx" && pwd)"

sudo install -d /etc/nginx/snippets
sudo install -m 644 "$HERE/jev-proxy.inc" /etc/nginx/snippets/jev-proxy.conf
sed "s/APP_HOST/$APP_HOST/g" "$HERE/jev-nav.conf" | sudo tee /etc/nginx/sites-available/jev-nav.conf >/dev/null
sudo ln -sf /etc/nginx/sites-available/jev-nav.conf /etc/nginx/sites-enabled/jev-nav.conf
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t
if [ "${2:-}" != "--no-reload" ]; then sudo systemctl reload nginx; fi
echo "nginx config installed for $APP_HOST"
