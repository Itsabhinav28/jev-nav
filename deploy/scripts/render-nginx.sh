#!/usr/bin/env bash
# Render the nginx config for your domain and install it.
#   bash deploy/scripts/render-nginx.sh example.com [--no-reload]
# nginx -t needs the certificate files: run certbot first (DEPLOY.md step 8).
set -euo pipefail

DOMAIN="${1:-}"
[ -n "$DOMAIN" ] || { echo "usage: $0 example.com [--no-reload]" >&2; exit 1; }
case "$DOMAIN" in *[!A-Za-z0-9.-]*) echo "invalid domain" >&2; exit 1;; esac
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../nginx" && pwd)"

sudo install -d /etc/nginx/snippets
sudo install -m 644 "$HERE/jev-proxy.inc" /etc/nginx/snippets/jev-proxy.conf
sed "s/DOMAIN/$DOMAIN/g" "$HERE/jev-nav.conf" | sudo tee /etc/nginx/sites-available/jev-nav.conf >/dev/null
sudo ln -sf /etc/nginx/sites-available/jev-nav.conf /etc/nginx/sites-enabled/jev-nav.conf
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t
if [ "${2:-}" != "--no-reload" ]; then sudo systemctl reload nginx; fi
echo "nginx config installed for $DOMAIN"
