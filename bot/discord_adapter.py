"""
discord_adapter.py - thin layer that connects Discord to the Brain.
Free, no business verification, no webhook needed (uses Discord's gateway
connection, same "connects out and listens" model as Telegram).

Works in two places:
  - DMs to the bot (just you, simplest setup)
  - Any server channel the bot's been invited to, if you allowlist it

Only listens to messages from IDs in ALLOWED_DISCORD_IDS.
"""
import logging
import discord

logger = logging.getLogger("discord_adapter")


class DiscordAdapter:
    def __init__(self, token: str, allowed_ids: set, brain):
        self.token = token
        self.allowed_ids = allowed_ids
        self.brain = brain

        intents = discord.Intents.default()
        intents.message_content = True  # required to read command text
        self.client = discord.Client(intents=intents)
        self._known_channel_ids = set()

        @self.client.event
        async def on_ready():
            logger.info(f"Discord adapter logged in as {self.client.user}")

        @self.client.event
        async def on_message(message: discord.Message):
            # Ignore the bot's own messages
            if message.author.id == self.client.user.id:
                return
            if not self._is_allowed(message.author.id):
                try:
                    await message.channel.send("Not authorized.")
                except Exception as e:
                    logger.error(f"Failed to send 'not authorized' message: {e}")
                return
            self._known_channel_ids.add(message.channel.id)
            try:
                reply = self.brain.handle(message.content)
                if not reply:
                    reply = "(No response generated - this is a bug)"
                await message.channel.send(reply)
                logger.info(f"Discord {message.author.id}: {message.content[:50]} -> OK")
            except Exception as e:
                logger.error(f"Failed to handle Discord message from {message.author.id}: {e}", exc_info=True)
                try:
                    await message.channel.send(f"Error: {e}")
                except Exception as e2:
                    logger.error(f"Failed to send error message: {e2}")

    def _is_allowed(self, user_id: int) -> bool:
        if not self.allowed_ids:
            # No allowlist configured - warn but allow (dev convenience).
            return True
        return user_id in self.allowed_ids

    def send_to_all_known(self, text: str):
        """
        Used by the scheduler (a different thread/event loop) to push
        proactive notifications to every channel/DM the bot has seen activity in.
        """
        import asyncio

        async def _send_all():
            success_count = 0
            for channel_id in self._known_channel_ids:
                try:
                    channel = self.client.get_channel(channel_id)
                    if channel is None:
                        channel = await self.client.fetch_channel(channel_id)
                    await channel.send(text)
                    success_count += 1
                except Exception as e:
                    logger.warning(f"Failed to notify Discord channel {channel_id}: {e}")
            logger.info(f"Sent notification to {success_count}/{len(self._known_channel_ids)} Discord channels")

        if not self._known_channel_ids:
            return

        loop = getattr(self.client, "loop", None)
        if loop and loop.is_running():
            # We're called from a different thread than the bot's event loop
            asyncio.run_coroutine_threadsafe(_send_all(), loop)
        else:
            asyncio.run(_send_all())

    def run(self):
        logger.info("Starting Discord client...")
        try:
            self.client.run(self.token, log_handler=None)
        except Exception as e:
            logger.error(f"Discord client failed: {e}", exc_info=True)
        finally:
            logger.info("Discord client stopped")
