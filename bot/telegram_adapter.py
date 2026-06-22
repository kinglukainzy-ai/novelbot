"""
telegram_adapter.py - thin layer that connects Telegram to the Brain.
Only listens to messages from IDs in ALLOWED_TELEGRAM_IDS.
"""
import logging
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, MessageHandler, ContextTypes, filters

logger = logging.getLogger("telegram_adapter")

TELEGRAM_MAX_LEN = 4096


def _chunk_text(text: str, max_len: int = TELEGRAM_MAX_LEN):
    """Split text into <= max_len chunks, breaking on newlines where possible
    so list-style output doesn't get cut mid-line."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


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

    async def _reply_safe(self, message, text: str):
        """Send a reply, chunked for length, falling back to plain text if
        HTML parsing fails (e.g. malformed tags in content)."""
        for chunk in _chunk_text(text):
            try:
                await message.reply_text(chunk, parse_mode="HTML")
            except BadRequest as e:
                logger.warning(f"HTML parse failed ({e}), retrying as plain text")
                await message.reply_text(chunk)

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat_id = update.effective_chat.id
        if not self._is_allowed(user.id):
            try:
                await update.message.reply_text("Not authorized.")
            except Exception as e:
                logger.error(f"Failed to send 'not authorized' message to {user.id}: {e}")
            return
        self._known_chat_ids.add(chat_id)
        try:
            reply = self.brain.handle(update.message.text)
            if not reply:
                reply = "(No response generated - this is a bug)"
            await self._reply_safe(update.message, reply)
            logger.info(f"Telegram {user.id}: {update.message.text[:50]} -> OK")
        except Exception as e:
            logger.error(f"Failed to handle Telegram message from {user.id}: {e}", exc_info=True)
            try:
                await update.message.reply_text(f"Error: {e}")
            except Exception as e2:
                logger.error(f"Failed to send error message to {user.id}: {e2}")

    def send_to_all_known(self, text: str):
        """
        Used by the scheduler (a different thread) to push proactive
        notifications. Creates its own short-lived event loop since this
        runs outside the bot's polling loop - fine for infrequent alerts.
        """
        import asyncio

        async def _send_all():
            success_count = 0
            for chat_id in self._known_chat_ids:
                for chunk in _chunk_text(text):
                    try:
                        await self.app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
                    except BadRequest as e:
                        logger.warning(f"HTML parse failed for chat {chat_id} ({e}), retrying as plain text")
                        try:
                            await self.app.bot.send_message(chat_id=chat_id, text=chunk)
                        except Exception as e2:
                            logger.warning(f"Failed to notify Telegram chat {chat_id}: {e2}")
                            break
                    except Exception as e:
                        logger.warning(f"Failed to notify Telegram chat {chat_id}: {e}")
                        break
                else:
                    success_count += 1
            logger.info(f"Sent notification to {success_count}/{len(self._known_chat_ids)} Telegram chats")

        if self._known_chat_ids:
            asyncio.run(_send_all())

    def run_polling(self):
        logger.info("Starting Telegram polling...")
        import asyncio
        # run_polling() calls asyncio.get_event_loop() internally, which only
        # auto-creates a loop on the main thread (Python 3.10+ behavior).
        # Since this runs in a background thread, we must create and set one
        # explicitly before calling run_polling().
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # stop_signals=None: registering OS signal handlers (SIGINT/SIGTERM)
        # only works in the main thread of the main interpreter. This adapter
        # runs in a background thread, so we disable that and let main.py's
        # main thread handle shutdown instead.
        try:
            self.app.run_polling(stop_signals=None)
        except Exception as e:
            logger.error(f"Telegram polling failed: {e}", exc_info=True)
        finally:
            logger.info("Telegram polling stopped")