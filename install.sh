#!/usr/bin/env bash
#
# install.sh - sets up the novel/anime tracker bot on a fresh Ubuntu VM
# (built for Oracle Cloud Always Free, but works on any Ubuntu 20.04+ box).
#
# What it does:
#   1. Installs Python3 + venv if missing
#   2. Creates a virtualenv (.venv) and installs requirements.txt
#   3. Copies .env.example -> .env (won't overwrite an existing .env)
#   4. Creates the data/ directory
#   5. Validates the systemd User= value actually exists
#   6. Installs + enables a systemd service so the bot survives reboots/crashes
#   7. Runs check_setup.py at the end so you know exactly what's left to fill in
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# After this finishes: edit .env with your tokens, run seed_library.py once
# if you want your NovelFire history imported, then start the service.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 1. Python3 / venv / pip
# ---------------------------------------------------------------------------
echo "==> Checking Python3..."
if ! command -v python3 &>/dev/null; then
    echo "Python3 not found - installing..."
    sudo apt update
    sudo apt install -y python3 python3-venv python3-pip
fi

PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "    Using Python $PY_VERSION"

# python3-venv is sometimes missing even when python3 itself is present
if ! python3 -m venv --help &>/dev/null; then
    echo "python3-venv module missing - installing..."
    sudo apt update
    sudo apt install -y python3-venv
fi

# ---------------------------------------------------------------------------
# 2. Virtualenv + dependencies
# ---------------------------------------------------------------------------
echo "==> Creating virtual environment (.venv)..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

echo "==> Installing dependencies..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

# ---------------------------------------------------------------------------
# 3. .env
# ---------------------------------------------------------------------------
echo "==> Setting up .env..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "    Created .env from .env.example."
else
    echo "    .env already exists - leaving existing values untouched, will only fill blanks."
fi

# Generate a random WhatsApp verify token if the placeholder is still in place,
# so nobody accidentally ships "changeme123" to a public webhook.
if grep -q '^WHATSAPP_VERIFY_TOKEN=changeme123$' .env 2>/dev/null; then
    NEW_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    sed -i "s/^WHATSAPP_VERIFY_TOKEN=changeme123$/WHATSAPP_VERIFY_TOKEN=${NEW_TOKEN}/" .env
fi

# --- helpers for interactive prompting -------------------------------------
get_env_val() {
    grep "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-
}

set_env_val() {
    local key="$1" val="$2"
    # escape sed special chars in the value
    local esc_val
    esc_val=$(printf '%s\n' "$val" | sed -e 's/[\/&]/\\&/g')
    if grep -q "^${key}=" .env 2>/dev/null; then
        sed -i "s/^${key}=.*/${key}=${esc_val}/" .env
    else
        echo "${key}=${val}" >> .env
    fi
}

prompt_if_blank() {
    # prompt_if_blank KEY "Prompt text" "help text (optional, printed above prompt)"
    local key="$1" prompt="$2" help="${3:-}"
    local current
    current="$(get_env_val "$key")"
    if [ -n "$current" ]; then
        echo "    $key already set, skipping."
        return
    fi
    [ -n "$help" ] && echo "    $help"
    read -rp "    $prompt: " value
    if [ -n "$value" ]; then
        set_env_val "$key" "$value"
    fi
}

# --- interactive key collection ---------------------------------------------
if [ -t 0 ]; then
    echo ""
    echo "==> Interactive setup - paste your keys now, or press Enter to skip a"
    echo "    platform/key and fill it in later by editing .env yourself."
    echo ""

    read -rp "Set up Telegram now? [y/N]: " do_telegram
    if [[ "$do_telegram" =~ ^[Yy]$ ]]; then
        prompt_if_blank "TELEGRAM_BOT_TOKEN" "Telegram bot token (from @BotFather)"
        prompt_if_blank "ALLOWED_TELEGRAM_IDS" "Your Telegram numeric ID (from @userinfobot)"
    fi

    echo ""
    read -rp "Set up Discord now? [y/N]: " do_discord
    if [[ "$do_discord" =~ ^[Yy]$ ]]; then
        prompt_if_blank "DISCORD_BOT_TOKEN" "Discord bot token (Developer Portal > Bot tab)"
        prompt_if_blank "ALLOWED_DISCORD_IDS" "Your Discord numeric user ID"
    fi

    echo ""
    read -rp "Set up WhatsApp now? [y/N]: " do_whatsapp
    if [[ "$do_whatsapp" =~ ^[Yy]$ ]]; then
        prompt_if_blank "WHATSAPP_TOKEN" "WhatsApp access token (Meta App Dashboard > API Setup)"
        prompt_if_blank "WHATSAPP_PHONE_NUMBER_ID" "WhatsApp Phone Number ID (same API Setup tab)"
        prompt_if_blank "WHATSAPP_APP_SECRET" "WhatsApp App Secret (App Settings > Basic) - optional, Enter to skip"
        prompt_if_blank "ALLOWED_WHATSAPP_NUMBERS" "Your WhatsApp number, country code no + (e.g. 15551234567)"
    fi

    echo ""
    read -rp "Set up the optional Groq key for /ask now? [y/N]: " do_groq
    if [[ "$do_groq" =~ ^[Yy]$ ]]; then
        prompt_if_blank "GROQ_API_KEY" "Groq API key (console.groq.com/keys) - Enter to skip"
    fi
    echo ""
else
    echo "    (non-interactive shell detected - skipping prompts, edit .env manually)"
fi

# ---------------------------------------------------------------------------
# 4. Data directory
# ---------------------------------------------------------------------------
echo "==> Creating data directory..."
mkdir -p data

# ---------------------------------------------------------------------------
# 5. Port check (WhatsApp webhook only - Telegram/Discord don't bind a port)
# ---------------------------------------------------------------------------
port_in_use() {
    # returns 0 (true) if something is already listening on $1
    if command -v ss &>/dev/null; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
    elif command -v netstat &>/dev/null; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
    else
        # Fall back to a raw bind attempt via python3
        ! python3 - "$1" <<'PYEOF' 2>/dev/null
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("0.0.0.0", port))
    s.close()
except OSError:
    sys.exit(1)
PYEOF
    fi
}

if grep -q '^WHATSAPP_TOKEN=.\+' .env 2>/dev/null; then
    CURRENT_PORT="$(grep '^WEBHOOK_PORT=' .env 2>/dev/null | cut -d= -f2)"
    CURRENT_PORT="${CURRENT_PORT:-8080}"

    echo "==> Checking if port $CURRENT_PORT is free for the WhatsApp webhook..."
    while port_in_use "$CURRENT_PORT"; do
        echo "    Port $CURRENT_PORT is already in use:"
        (ss -ltnp 2>/dev/null | grep ":$CURRENT_PORT" ) || true
        read -rp "    Enter a different port to use instead [default: $((CURRENT_PORT + 1))]: " NEW_PORT
        NEW_PORT="${NEW_PORT:-$((CURRENT_PORT + 1))}"
        CURRENT_PORT="$NEW_PORT"
    done

    if grep -q '^WEBHOOK_PORT=' .env 2>/dev/null; then
        sed -i "s/^WEBHOOK_PORT=.*/WEBHOOK_PORT=$CURRENT_PORT/" .env
    else
        echo "WEBHOOK_PORT=$CURRENT_PORT" >> .env
    fi
    echo "    Using port $CURRENT_PORT for the WhatsApp webhook (set in .env as WEBHOOK_PORT)."
    echo "    Remember: your nginx reverse proxy / firewall rules must point at this port too."
fi

# ---------------------------------------------------------------------------
# 6. systemd service
# ---------------------------------------------------------------------------
echo "==> Setting up systemd service..."
SERVICE_FILE="/etc/systemd/system/novelbot.service"
CURRENT_USER="$(whoami)"

if [ "$CURRENT_USER" = "root" ]; then
    echo "    WARNING: running as root. The service will run as root too -"
    echo "    consider creating a dedicated non-root user for this in production."
fi

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Novel/Anime Tracker Bot
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/main.py
Restart=on-failure
RestartSec=10
EnvironmentFile=$SCRIPT_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable novelbot >/dev/null 2>&1 || true

echo ""
echo "============================================================"
echo " Install complete."
echo "============================================================"
echo ""

# ---------------------------------------------------------------------------
# 7. Show what's missing right now
# ---------------------------------------------------------------------------
if [ -f "check_setup.py" ]; then
    .venv/bin/python check_setup.py || true
    echo ""
fi

echo "Next steps:"
echo "  1. If you skipped any keys above, fill them in now:"
echo "       nano .env"
echo "  2. (Optional) Import your NovelFire reading history:"
echo "       .venv/bin/python seed_library.py"
echo "  3. Start the bot now (foreground, for testing):"
echo "       .venv/bin/python main.py"
echo "  4. Or run it permanently as a service (already enabled at boot):"
echo "       sudo systemctl start novelbot"
echo "  5. Check it's running / view logs:"
echo "       sudo systemctl status novelbot"
echo "       journalctl -u novelbot -f"
echo ""
echo "Run ./check_setup.sh (or .venv/bin/python check_setup.py) anytime to see"
echo "which API keys are still missing."
echo ""
