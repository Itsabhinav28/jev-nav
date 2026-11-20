# Shared proxy settings (installed as /etc/nginx/snippets/jev-proxy.conf).
# The app has NO login of its own. Access is by a secret link instead of a password:
# open https://APP_HOST/?k=SECRET once; nginx then sets a cookie and the browser stays in.
# Without the key or the cookie every request gets 403 (except /healthz). The maps behind this
# ($jev_allowed, $jev_set_cookie) live in /etc/nginx/jev-access.conf, written by make-access-key.sh.
if ($jev_allowed = 0) { return 403 "Access link required.\n"; }
add_header Set-Cookie $jev_set_cookie always;

# --- Optional extra: also (or only) allow your own IPs. Combine with the key check above by
# putting `allow`/`deny` here and `satisfy any;` if either should be enough.
# allow 203.0.113.10;   # your home/office IP
# deny  all;

# Security headers (no HSTS preload; add includeSubDomains only once all subdomains are HTTPS)
add_header Strict-Transport-Security "max-age=31536000" always;
add_header X-Content-Type-Options    "nosniff" always;
add_header X-Frame-Options           "SAMEORIGIN" always;
add_header Referrer-Policy           "no-referrer" always;
# Camera is needed by /phone; everything else denied
add_header Permissions-Policy        "camera=(self), microphone=(self), geolocation=(), payment=()" always;

# Signalling POST /offer carries an SDP; uploads are small. /api/ask may carry a short text/image.
client_max_body_size 10m;

proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
# WebSocket upgrade (not used by the app today; harmless if it is)
proxy_set_header Upgrade           $http_upgrade;
proxy_set_header Connection        $connection_upgrade;

proxy_connect_timeout 5s;
proxy_send_timeout    120s;
proxy_read_timeout    120s;   # Jev / OpenRouter calls can take several seconds
