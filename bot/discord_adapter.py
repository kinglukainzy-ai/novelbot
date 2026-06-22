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
                await message.channel.send("Not authorized.")
                return
            self._known_channel_ids.add(message.channel.id)
            reply = self.brain.handle(message.content)
            await message.channel.send(reply)

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
            for channel_id in self._known_channel_ids:
                try:
                    channel = self.client.get_channel(channel_id)
                    if channel is None:
                        channel = await self.client.fetch_channel(channel_id)
                    await channel.send(text)
                except Exception as e:
                    logger.warning(f"Failed to notify Discord channel {channel_id}: {e}")

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
        self.client.run(self.token, log_handler=None)
