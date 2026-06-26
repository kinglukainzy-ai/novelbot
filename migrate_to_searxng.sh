#!/usr/bin/env bash
# migrate_to_searxng.sh - one-shot migration off the Gemini/Tavily/Groq
# web-search APIs onto a self-hosted SearXNG instance.
#
# What this does, in order:
#   1. Starts the SearXNG container (docker-compose.searxng.yml).
#   2. Enables its JSON API (off by default) and restarts it so the
#      setting takes effect.
#   3. Confirms the JSON endpoint actually responds before touching .env.
#   4. Sets SEARXNG_URL in .env.
#   5. Comments out GEMINI_API_KEY / TAVILY_API_KEY / GROQ_API_KEY so the
#      bot stops depending on them - their values are kept, just disabled,
#      so you can roll back by uncommenting instead of re-entering keys.
#
# Safe to re-run any time (e.g. after pulling a newer SearXNG image).
#
# Usage:
#   ./migrate_to_searxng.sh                       # interactive
#   ENV_FILE=/path/to/.env ./migrate_to_searxng.sh
#   KEEP_LEGACY_KEYS=1 ./migrate_to_searxng.sh    # set SEARXNG_URL but
#                                                  # leave the old keys
#                                                  # active as a fallback
#                                                  # instead of commenting
#                                                  # them out

set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
COMPOSE_FILE="docker-compose.searxng.yml"
SEARXNG_PORT="${SEARXNG_PORT:-8888}"
SEARXNG_URL_VALUE="http://localhost:${SEARXNG_PORT}"

# ---------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "No .env file found at '$ENV_FILE'."
    echo "Copy .env.example to .env first, then re-run this script."
    exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "Can't find $COMPOSE_FILE - run this from the repo root."
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker isn't installed. Install it first: https://docs.docker.com/get-docker/"
    exit 1
fi

COMPOSE_CMD=(docker compose)
if ! docker compose version >/dev/null 2>&1; then
    if command -v docker-compose >/dev/null 2>&1; then
        COMPOSE_CMD=(docker-compose)
    else
        echo "Neither 'docker compose' nor 'docker-compose' is available."
        exit 1
    fi
fi

echo "==> Backing up $ENV_FILE to ${ENV_FILE}.bak"
cp "$ENV_FILE" "${ENV_FILE}.bak"

# ---------------------------------------------------------------------
# 1. Start SearXNG
# ---------------------------------------------------------------------
echo "==> Starting SearXNG (${COMPOSE_CMD[*]} -f $COMPOSE_FILE up -d)..."
"${COMPOSE_CMD[@]}" -f "$COMPOSE_FILE" up -d

echo "==> Waiting for the container to come up..."
for i in $(seq 1 15); do
    if curl -fsS "${SEARXNG_URL_VALUE}/" >/dev/null 2>&1; then
        break
    fi
    sleep 2
    if [[ "$i" -eq 15 ]]; then
        echo "SearXNG didn't come up after 30s - check 'docker logs novelbot-searxng'."
        exit 1
    fi
done
echo "    container is up."

# ---------------------------------------------------------------------
# 2. Enable JSON output (off by default) and restart
# ---------------------------------------------------------------------
echo "==> Enabling JSON API in settings.yml..."
"${COMPOSE_CMD[@]}" -f "$COMPOSE_FILE" exec -T searxng python3 - <<'PYEOF'
import re

PATH = "/etc/searxng/settings.yml"
with open(PATH) as f:
    content = f.read()

# Already enabled - nothing to do.
if re.search(r"formats:\s*\n(?:\s*-\s*\S+\s*\n)*\s*-\s*json\s*\n?", content):
    print("    json format already enabled")
else:
    # Common case: a `formats:` list already exists under `search:` (the
    # default settings.yml ships with `formats:\n    - html`). Add json
    # to that existing list rather than assuming exact indentation.
    def add_json(m):
        block = m.group(0)
        indent = re.search(r"\n(\s*)-\s*\S+", block).group(1)
        return block.rstrip("\n") + f"\n{indent}- json\n"

    new_content, n = re.subn(
        r"formats:\s*\n(?:\s*-\s*\S+\s*\n)+", add_json, content, count=1
    )
    if n == 0:
        # `search:` key exists but has no `formats:` list under it yet -
        # insert one right after the `search:` line.
        new_content, n = re.subn(
            r"(^search:\s*\n)", r"\1  formats:\n    - html\n    - json\n",
            content, count=1, flags=re.MULTILINE,
        )
    if n == 0:
        # No `search:` section in the file at all (common on a fresh
        # container - the on-disk file can be a minimal template that
        # relies on use_default_settings: true for everything else).
        # Append a new top-level section rather than giving up.
        if not content.endswith("\n"):
            content += "\n"
        new_content = content + "search:\n  formats:\n    - html\n    - json\n"
        n = 1

    with open(PATH, "w") as f:
        f.write(new_content)
    print("    added 'json' to search.formats")
PYEOF

echo "==> Restarting SearXNG so the setting takes effect..."
"${COMPOSE_CMD[@]}" -f "$COMPOSE_FILE" restart searxng

echo "==> Waiting for it to come back up..."
sleep 3
for i in $(seq 1 15); do
    if curl -fsS "${SEARXNG_URL_VALUE}/" >/dev/null 2>&1; then
        break
    fi
    sleep 2
    if [[ "$i" -eq 15 ]]; then
        echo "SearXNG didn't come back after restart - check 'docker logs novelbot-searxng'."
        exit 1
    fi
done

# ---------------------------------------------------------------------
# 3. Confirm the JSON endpoint actually works before touching .env
# ---------------------------------------------------------------------
echo "==> Verifying the JSON endpoint..."
JSON_CHECK="$(curl -fsS "${SEARXNG_URL_VALUE}/search?q=test&format=json" 2>&1 || true)"
if ! echo "$JSON_CHECK" | grep -q '"results"'; then
    echo "JSON endpoint isn't responding as expected. Output was:"
    echo "$JSON_CHECK" | head -c 500
    echo ""
    echo "Not touching .env - fix SearXNG first, then re-run this script."
    exit 1
fi
echo "    JSON endpoint confirmed working."

# ---------------------------------------------------------------------
# 4. Point the bot at it
# ---------------------------------------------------------------------
set_var() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i.tmp "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" && rm -f "${ENV_FILE}.tmp"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

echo "==> Setting SEARXNG_URL=${SEARXNG_URL_VALUE} in $ENV_FILE"
set_var "SEARXNG_URL" "$SEARXNG_URL_VALUE"

# ---------------------------------------------------------------------
# 5. Retire the legacy keys (commented out, not deleted - reversible)
# ---------------------------------------------------------------------
if [[ "${KEEP_LEGACY_KEYS:-0}" == "1" ]]; then
    echo "==> KEEP_LEGACY_KEYS=1 set - leaving GEMINI_API_KEY/TAVILY_API_KEY/GROQ_API_KEY active as a fallback."
else
    echo "==> Retiring legacy keys (commenting out, values preserved for rollback)..."
    for key in GEMINI_API_KEY TAVILY_API_KEY GROQ_API_KEY; do
        if grep -q "^${key}=.\+" "$ENV_FILE"; then
            sed -i.tmp "s|^${key}=\(.\+\)|# ${key}=\1  # disabled by migrate_to_searxng.sh - uncomment to restore|" "$ENV_FILE" \
                && rm -f "${ENV_FILE}.tmp"
            echo "    ${key}: disabled (was set)"
        fi
    done
fi

echo ""
echo "==> Done. Current state:"
grep -E "^SEARXNG_URL=|^#? ?(GEMINI_API_KEY|TAVILY_API_KEY|GROQ_API_KEY)=" "$ENV_FILE" \
    | sed -E 's/=([^#]{4}).*( #.*)?$/=\1****\2/'

echo ""
echo "Restart the bot for the change to take effect. Check it worked with /sources."
echo "Rollback: restore from ${ENV_FILE}.bak, or uncomment the legacy keys above."