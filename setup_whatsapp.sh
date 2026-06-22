#!/usr/bin/env bash
# setup_whatsapp.sh - Configure WhatsApp + nginx + SSL for novelbot
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[+]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[x]${NC} $*"; exit 1; }

ENV_FILE="$HOME/novelbot/.env"
[ -f "$ENV_FILE" ] || error ".env not found at $ENV_FILE — is novelbot installed?"

echo ""
echo "================================================"
echo "   novelbot — WhatsApp + Webhook Setup"
echo "================================================"
echo ""

# ── 1. Collect credentials ──────────────────────────────────────────────────
read -rp "Paste your WhatsApp Access Token: " WA_TOKEN
[ -z "$WA_TOKEN" ] && error "Token cannot be empty."

read -rp "Duck DNS subdomain (e.g. thoth — without .duckdns.org): " SUBDOMAIN
[ -z "$SUBDOMAIN" ] && error "Subdomain cannot be empty."
DOMAIN="${SUBDOMAIN}.duckdns.org"

read -rp "Duck DNS token (from duckdns.org): " DUCK_TOKEN
[ -z "$DUCK_TOKEN" ] && error "Duck DNS token cannot be empty."

read -rp "Your WhatsApp number (country code, no +, e.g. 233209611104): " WA_NUMBER
[ -z "$WA_NUMBER" ] && error "Number cannot be empty."

# ── 2. Update .env ───────────────────────────────────────────────────────────
info "Updating .env..."

set_env() {
    local key="$1" val="$2"
    local esc; esc=$(printf '%s\n' "$val" | sed -e 's/[\\/&]/\\&/g')
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${esc}|" "$ENV_FILE"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

set_env "WHATSAPP_TOKEN"           "$WA_TOKEN"
set_env "WHATSAPP_PHONE_NUMBER_ID" "1218878787966769"
set_env "ALLOWED_WHATSAPP_NUMBERS" "$WA_NUMBER"
set_env "WEBHOOK_PORT"             "8080"

# Grab the auto-generated verify token
VERIFY_TOKEN=$(grep '^WHATSAPP_VERIFY_TOKEN=' "$ENV_FILE" | cut -d= -f2)
[ -z "$VERIFY_TOKEN" ] && { VERIFY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))'); set_env "WHATSAPP_VERIFY_TOKEN" "$VERIFY_TOKEN"; }

info ".env updated."

# ── 3. Point Duck DNS to this server ─────────────────────────────────────────
info "Updating Duck DNS → $DOMAIN ..."
PUBLIC_IP=$(curl -s https://api.ipify.org)
DUCK_RESULT=$(curl -s "https://www.duckdns.org/update?domains=${SUBDOMAIN}&token=${DUCK_TOKEN}&ip=${PUBLIC_IP}")
if [[ "$DUCK_RESULT" == "OK" ]]; then
    info "Duck DNS updated to $PUBLIC_IP"
else
    warn "Duck DNS responded: $DUCK_RESULT — check your subdomain/token and retry if needed."
fi

# ── 4. Install nginx + certbot ────────────────────────────────────────────────
info "Installing nginx and certbot..."
sudo apt-get update -qq
sudo apt-get install -y nginx certbot python3-certbot-nginx -qq

# ── 5. Open firewall ports (Oracle Cloud iptables) ───────────────────────────
info "Opening ports 80 and 443 in iptables..."
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT 2>/dev/null || true
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
sudo netfilter-persistent save 2>/dev/null || true

# ── 6. Temporary nginx for certbot HTTP challenge ─────────────────────────────
info "Writing temporary nginx config for SSL certificate..."
sudo tee /etc/nginx/sites-available/novelbot > /dev/null <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        return 200 'ok';
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/novelbot /etc/nginx/sites-enabled/novelbot
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# ── 7. Get SSL cert ───────────────────────────────────────────────────────────
info "Obtaining SSL certificate for $DOMAIN ..."
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email

# ── 8. Final nginx config with webhook proxy ──────────────────────────────────
info "Writing final nginx config..."
sudo tee /etc/nginx/sites-available/novelbot > /dev/null <<EOF
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    location /webhook {
        proxy_pass         http://127.0.0.1:8080/webhook;
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo nginx -t && sudo systemctl reload nginx
info "nginx configured."

# ── 9. Restart novelbot ───────────────────────────────────────────────────────
info "Restarting novelbot service..."
sudo systemctl restart novelbot
sleep 3
sudo systemctl is-active --quiet novelbot && info "novelbot is running." || warn "novelbot may have failed — check: journalctl -u novelbot -f"

# ── 10. Summary ───────────────────────────────────────────────────────────────
echo ""
echo "================================================"
echo -e "${GREEN}   Setup complete!${NC}"
echo "================================================"
echo ""
echo "  Webhook URL:   https://${DOMAIN}/webhook"
echo "  Verify Token:  ${VERIFY_TOKEN}"
echo ""
echo "  Now go to Meta Dashboard:"
echo "  Thoth → Use cases → Customize → Configuration"
echo "  Click 'Edit' and enter:"
echo "    Callback URL:  https://${DOMAIN}/webhook"
echo "    Verify Token:  ${VERIFY_TOKEN}"
echo "  Then subscribe to the 'messages' webhook field."
echo ""
echo "  Also remember to open port 443 in your Oracle"
echo "  Cloud Security List if you haven't already:"
echo "  OCI Console → VCN → Security Lists → Add Ingress Rule"
echo "  Protocol: TCP, Port: 443, Source: 0.0.0.0/0"
echo ""
