"""
main.py - wires together the Brain, the Telegram adapter, the WhatsApp
adapter, and the scheduler. Run this on your always-on server (Oracle
Free VM).

    python main.py

Telegram runs its polling loop in a background thread; Flask (WhatsApp)
runs in the main thread so it can bind to a port behind your reverse proxy.
"""
import os
import threading
import logging
from dotenv import load_dotenv

from bot.brain import Brain
from bot.telegram_adapter import TelegramAdapter
from bot.whatsapp_adapter import WhatsAppAdapter
from bot.discord_adapter import DiscordAdapter
from bot.scheduler import start_scheduler

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
logger = logging.getLogger("main")


def csv_set(value):
    return {v.strip() for v in (value or "").split(",") if v.strip()}


def main():
    db_path = os.getenv("DATABASE_PATH", "data/bot.db")
    check_interval = int(os.getenv("CHECK_INTERVAL_MINUTES", "90"))

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    whatsapp_token = os.getenv("WHATSAPP_TOKEN")
    whatsapp_phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "changeme123")
    whatsapp_app_secret = os.getenv("WHATSAPP_APP_SECRET")

    allowed_telegram_ids = {int(x) for x in csv_set(os.getenv("ALLOWED_TELEGRAM_IDS")) if x.isdigit()}
    allowed_whatsapp_numbers = csv_set(os.getenv("ALLOWED_WHATSAPP_NUMBERS"))

    discord_token = os.getenv("DISCORD_BOT_TOKEN")
    allowed_discord_ids = {int(x) for x in csv_set(os.getenv("ALLOWED_DISCORD_IDS")) if x.isdigit()}

    if not telegram_token and not whatsapp_token and not discord_token:
        raise SystemExit(
            "Set at least TELEGRAM_BOT_TOKEN, WHATSAPP_TOKEN+WHATSAPP_PHONE_NUMBER_ID, "
            "or DISCORD_BOT_TOKEN in .env"
        )

    brain = Brain(db_path)

    adapters = []

    telegram_adapter = None
    if telegram_token:
        telegram_adapter = TelegramAdapter(telegram_token, allowed_telegram_ids, brain)
        adapters.append(telegram_adapter)

    whatsapp_adapter = None
    if whatsapp_token and whatsapp_phone_id:
        if not whatsapp_app_secret:
            logger.warning(
                "WHATSAPP_APP_SECRET not set - incoming webhook requests won't "
                "be signature-verified. Fine for local testing, but set this "
                "before exposing the webhook publicly."
            )
        whatsapp_adapter = WhatsAppAdapter(
            whatsapp_token, whatsapp_phone_id, whatsapp_verify_token,
            allowed_whatsapp_numbers, brain, app_secret=whatsapp_app_secret,
        )
        adapters.append(whatsapp_adapter)

    discord_adapter = None
    if discord_token:
        discord_adapter = DiscordAdapter(discord_token, allowed_discord_ids, brain)
        adapters.append(discord_adapter)

    def notify_all(text):
        for a in adapters:
            a.send_to_all_known(text)

    brain.notifier = notify_all

    start_scheduler(brain, check_interval)
    logger.info(f"Scheduler started, checking every {check_interval} minutes.")

    if telegram_adapter:
        t = threading.Thread(target=telegram_adapter.run_polling, daemon=True)
        t.start()
        logger.info("Telegram adapter running in background thread.")

    if discord_adapter:
        d = threading.Thread(target=discord_adapter.run, daemon=True)
        d.start()
        logger.info("Discord adapter running in background thread.")

    if whatsapp_adapter:
        webhook_port = int(os.getenv("WEBHOOK_PORT", "8080"))
        logger.info(f"Starting WhatsApp webhook server on port {webhook_port}...")
        whatsapp_adapter.run(port=webhook_port)
    else:
        # No WhatsApp webhook to serve - just keep the main thread alive
        # so the Telegram/Discord background threads keep running.
        threading.Event().wait()


if __name__ == "__main__":
    main()
