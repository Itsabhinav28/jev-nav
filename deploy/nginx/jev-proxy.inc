# Shared proxy settings for every jev instance (installed as /etc/nginx/snippets/jev-proxy.conf).
# The app has NO login of its own: basic auth below is mandatory on a public IP.
auth_basic           "jev-nav";
auth_basic_user_file /etc/nginx/.htpasswd;

# --- Optional alternative: allow only your own IPs instead of (or in addition to) a password.
# With "satisfy any" either a valid IP OR a valid password gets in; "satisfy all" requires both.
# satisfy any;
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
