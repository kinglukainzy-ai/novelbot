#!/usr/bin/env bash
# set_gemini_key.sh - add or update GEMINI_API_KEY (and optionally
# GEMINI_MODEL) in an existing .env file, then install the Python deps
# needed to actually use it (currently just google-genai). Safe to re-run
# any time you want to rotate the key or re-sync deps.
#
# Usage:
#   ./set_gemini_key.sh                 # prompts for the key
#   ./set_gemini_key.sh AIzaSy...       # pass the key directly
#   ENV_FILE=/path/to/.env ./set_gemini_key.sh   # target a different file
#   SKIP_INSTALL=1 ./set_gemini_key.sh  # only touch .env, skip pip install

set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "No .env file found at '$ENV_FILE'."
    echo "Copy .env.example to .env first (or set ENV_FILE to point at the right path), then re-run this script."
    exit 1
fi

# ---- get the key, either from $1 or interactively ----
if [[ $# -ge 1 ]]; then
    GEMINI_KEY="$1"
else
    read -rp "Gemini API key (get one free at https://aistudio.google.com/apikey): " GEMINI_KEY
fi

if [[ -z "$GEMINI_KEY" ]]; then
    echo "No key entered - nothing changed."
    exit 1
fi

# ---- optional model override ----
read -rp "Gemini model [Enter for default: gemini-2.0-flash]: " GEMINI_MODEL_INPUT
GEMINI_MODEL_VALUE="${GEMINI_MODEL_INPUT:-gemini-2.0-flash}"

# ---- backup before touching anything ----
cp "$ENV_FILE" "${ENV_FILE}.bak"
echo "Backed up existing file to ${ENV_FILE}.bak"

set_var() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        # Update existing line in place.
        sed -i.tmp "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" && rm -f "${ENV_FILE}.tmp"
    else
        # Append a new line if it wasn't there at all.
        printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

set_var "GEMINI_API_KEY" "$GEMINI_KEY"
set_var "GEMINI_MODEL" "$GEMINI_MODEL_VALUE"

echo "Updated $ENV_FILE:"
grep -E "^GEMINI_API_KEY=|^GEMINI_MODEL=" "$ENV_FILE" | sed -E 's/^(GEMINI_API_KEY=).{4}.*/\1****(hidden)/'

# ---- install the Python deps this feature needs ----
if [[ "${SKIP_INSTALL:-0}" == "1" ]]; then
    echo ""
    echo "SKIP_INSTALL set - leaving Python deps alone."
else
    echo ""
    echo "Installing/updating google-genai..."

    PIP_CMD="pip3"
    command -v pip3 >/dev/null 2>&1 || PIP_CMD="pip"

    if "$PIP_CMD" install --break-system-packages -U google-genai; then
        echo "google-genai installed/updated."
    else
        echo "First install attempt failed, retrying without --break-system-packages (e.g. inside a venv)..."
        "$PIP_CMD" install -U google-genai
    fi

    # If a full requirements.txt is sitting next to this script, sync against
    # it too so nothing else silently drifts out of date.
    REQ_FILE="$SCRIPT_DIR/requirements.txt"
    if [[ -f "$REQ_FILE" ]]; then
        echo "Syncing the rest of requirements.txt too..."
        "$PIP_CMD" install --break-system-packages -r "$REQ_FILE" 2>/dev/null \
            || "$PIP_CMD" install -r "$REQ_FILE"
    fi
fi

echo ""
echo "Restart the bot for the change to take effect."
