#!/usr/bin/env bash
# Create (or rotate) the secret that replaces the password: people open
#   https://APP_HOST/?k=SECRET
# once; nginx sets a cookie and the browser stays logged in for a year.
#   bash deploy/scripts/make-access-key.sh app.example.com [--no-reload]
# Run again to rotate: the old link and all old cookies stop working immediately.
set -euo pipefail

APP_HOST="${1:-}"
[ -n "$APP_HOST" ] || { echo "usage: $0 app.example.com [--no-reload]" >&2; exit 1; }
case "$APP_HOST" in *[!A-Za-z0-9.-]*) echo "invalid hostname" >&2; exit 1;; esac

KEY="$(openssl rand -hex 16)"
sudo tee /etc/nginx/jev-access.conf >/dev/null <<EOF
# Written by make-access-key.sh. Do not commit. Rotate by running the script again.
map \$cookie_jevkey \$jev_cookie_ok { default 0; "$KEY" 1; }
map \$arg_k         \$jev_arg_ok    { default 0; "$KEY" 1; }
map \$uri           \$jev_open      { default 0; /healthz 1; }
map "\$jev_cookie_ok\$jev_arg_ok\$jev_open" \$jev_allowed { default 0; ~1 1; }
map \$arg_k \$jev_set_cookie { default ""; "$KEY" "jevkey=$KEY; Path=/; Max-Age=31536000; Secure; HttpOnly; SameSite=Lax"; }
EOF
sudo chown root:www-data /etc/nginx/jev-access.conf
sudo chmod 640 /etc/nginx/jev-access.conf

if [ "${2:-}" != "--no-reload" ] && [ -f /etc/nginx/sites-enabled/jev-nav.conf ]; then
    sudo nginx -t && sudo systemctl reload nginx
fi

echo
echo "Share these links (anyone with the link gets in, no password):"
echo "  Dashboard : https://$APP_HOST/?k=$KEY"
echo "  Phone     : https://$APP_HOST/phone?k=$KEY"
