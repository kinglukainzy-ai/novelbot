"""
telegram_adapter.py - thin layer that connects Telegram to the Brain.
Only listens to messages from IDs in ALLOWED_TELEGRAM_IDS.
"""
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

logger = logging.getLogger("telegram_adapter")


class TelegramAdapter:
    def __init__(self, token: str, allowed_ids: set, brain):
        self.token = token
        self.allowed_ids = allowed_ids
        self.brain = brain
        self.app = Application.builder().token(token).build()
        self.app.add_handler(MessageHandler(filters.TEXT, self._on_message))
        self._known_chat_ids = set()

    def _is_allowed(self, user_id: int) -> bool:
        if not self.allowed_ids:
            # No allowlist configured - warn but allow (dev convenience).
            return True
        return user_id in self.allowed_ids

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat_id = update.effective_chat.id
        if not self._is_allowed(user.id):
            await update.message.reply_text("Not authorized.")
            return
        self._known_chat_ids.add(chat_id)
        reply = self.brain.handle(update.message.text)
        await update.message.reply_text(reply)

    def send_to_all_known(self, text: str):
        """
        Used by the scheduler (a different thread) to push proactive
        notifications. Creates its own short-lived event loop since this
        runs outside the bot's polling loop - fine for infrequent alerts.
        """
        import asyncio

        async def _send_all():
            for chat_id in self._known_chat_ids:
                try:
                    await self.app.bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    logger.warning(f"Failed to notify Telegram chat {chat_id}: {e}")

        if self._known_chat_ids:
            asyncio.run(_send_all())

    def run_polling(self):
        logger.info("Starting Telegram polling...")
        self.app.run_polling()
