#!/usr/bin/env python3
"""
check_setup.py - reads your .env and tells you exactly what's missing,
what's optional, and where to get each key.

Usage:
    python check_setup.py
"""
import os
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"

# (key, used_for, where_to_get)
CHECKS = [
    ("TELEGRAM_BOT_TOKEN", "Telegram platform",
     "Message @BotFather on Telegram, send /newbot, follow the prompts."),
    ("ALLOWED_TELEGRAM_IDS", "Telegram access control",
     "Message @userinfobot on Telegram to get your numeric user ID."),

    ("DISCORD_BOT_TOKEN", "Discord platform",
     "https://discord.com/developers/applications -> New Application -> Bot tab -> Reset Token. "
     "Enable 'Message Content Intent' under Privileged Gateway Intents."),
    ("ALLOWED_DISCORD_IDS", "Discord access control",
     "Enable Developer Mode in Discord (Settings > Advanced), right-click your name > Copy User ID."),

    ("WHATSAPP_TOKEN", "WhatsApp platform",
     "https://developers.facebook.com -> create app -> add WhatsApp product -> API Setup tab "
     "gives a temporary token (24h), or generate a permanent token under "
     "Business Settings > Users > System Users."),
    ("WHATSAPP_PHONE_NUMBER_ID", "WhatsApp platform",
     "Same Meta App Dashboard, WhatsApp > API Setup tab."),
    ("WHATSAPP_APP_SECRET", "WhatsApp webhook security",
     "Meta App Dashboard > App Settings > Basic > App Secret. "
     "Without this, incoming webhook requests aren't signature-verified - "
     "fine for local testing, set it before exposing the webhook publicly."),
    ("WHATSAPP_VERIFY_TOKEN", "WhatsApp webhook setup",
     "Any random string - install.sh auto-generates one for you. "
     "You'll re-enter this same value in the Meta webhook config screen."),
    ("ALLOWED_WHATSAPP_NUMBERS", "WhatsApp access control",
     "Your own WhatsApp number (with country code, no +), verified as a test "
     "recipient in the same API Setup tab."),

    ("GROQ_API_KEY", "/ask natural-language command (optional)",
     "https://console.groq.com/keys - free tier, no card required. "
     "Leave blank to disable /ask; every other command still works for free."),
]

PLATFORM_GROUPS = {
    "Telegram": ["TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_IDS"],
    "Discord": ["DISCORD_BOT_TOKEN", "ALLOWED_DISCORD_IDS"],
    "WhatsApp": ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
                 "WHATSAPP_APP_SECRET", "ALLOWED_WHATSAPP_NUMBERS"],
}

# Only these are ever truly "required" - and only one platform's set is
# needed, not all of them. Everything else (GROQ, app secret, verify token)
# is optional/security-hardening.
OPTIONAL_KEYS = {"GROQ_API_KEY", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"}


def load_env():
    values = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        values[k.strip()] = v.strip()
    return values


def main():
    if not ENV_PATH.exists():
        print("No .env file found. Run ./install.sh first, or copy .env.example to .env.")
        return

    env = load_env()
    present, missing = [], []

    for key, used_for, where in CHECKS:
        if env.get(key):
            present.append(key)
        else:
            missing.append((key, used_for, where))

    print("=" * 60)
    print(" Setup check")
    print("=" * 60)
    print(f"\nSet: {len(present)}/{len(CHECKS)} keys")
    for k in present:
        print(f"  [x] {k}")

    if missing:
        print(f"\nMissing: {len(missing)} keys\n")
        for key, used_for, where in missing:
            tag = "optional" if key in OPTIONAL_KEYS else "needed for that platform"
            print(f"  [ ] {key}  ({tag} - {used_for})")
            print(f"      -> {where}\n")
    else:
        print("\nAll keys are set.")

    print("=" * 60)
    print(" Platform readiness")
    print("=" * 60)
    any_ready = False
    for platform, keys in PLATFORM_GROUPS.items():
        required_keys = [k for k in keys if k not in OPTIONAL_KEYS]
        ready = all(env.get(k) for k in required_keys)
        any_ready = any_ready or ready
        status = "READY" if ready else "not configured"
        print(f"  {platform:10s} - {status}")

    print()
    if any_ready:
        print("At least one platform is configured - the bot can run.")
    else:
        print("No platform is fully configured yet - the bot will refuse to start.")
        print("You only need ONE of Telegram / Discord / WhatsApp, not all three.")
    print("GROQ_API_KEY is optional - only needed for the /ask command.")

    if env.get("WHATSAPP_TOKEN"):
        port = env.get("WEBHOOK_PORT", "8080")
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("0.0.0.0", int(port)))
            s.close()
            print(f"\nWHATSAPP_PORT check: port {port} is free.")
        except OSError:
            print(f"\nWHATSAPP_PORT check: port {port} is already in use! "
                  f"Re-run ./install.sh to pick a different one, or edit "
                  f"WEBHOOK_PORT in .env manually.")

    print("=" * 60)


if __name__ == "__main__":
    main()
