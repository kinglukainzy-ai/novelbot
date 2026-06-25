"""
discord_adapter.py - Discord adapter with native slash command support.
Commands show up in Discord's autocomplete menu when you type /.
"""
import logging
import asyncio
import discord
from discord import app_commands

logger = logging.getLogger(__name__)

DISCORD_MAX_LEN = 2000


def _html_to_discord(text: str) -> str:
    text = text.replace("<b>", "**").replace("</b>", "**")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text


def _chunk_text(text: str, max_len: int = DISCORD_MAX_LEN):
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


class DiscordAdapter:
    def __init__(self, token: str, allowed_ids: set, brain):
        self.token = token
        self.allowed_ids = allowed_ids
        self.brain = brain

        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        self._known_channel_ids = set()

        self._register_commands()

        @self.client.event
        async def on_ready():
            await self.tree.sync()
            logger.info(f"Discord adapter logged in as {self.client.user} — slash commands synced")

        # Keep text commands working too (fallback / DM support)
        @self.client.event
        async def on_message(message: discord.Message):
            if message.author.id == self.client.user.id:
                return
            if not self._is_allowed(message.author.id):
                await message.channel.send("Not authorized.")
                return
            # Only handle text-style /commands (not slash interactions, those go through tree)
            if not message.content.startswith("/"):
                return
            self._known_channel_ids.add(message.channel.id)
            await self._run_and_reply(message.channel, message.content, message.author.id)

    def _is_allowed(self, user_id: int) -> bool:
        if not self.allowed_ids:
            return True
        return user_id in self.allowed_ids

    async def _run_and_reply(self, channel, text: str, user_id: int):
        try:
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(None, self.brain.handle, text, user_id)
            if not reply:
                reply = "(No response)"
            reply_md = _html_to_discord(reply)
            for chunk in _chunk_text(reply_md):
                await channel.send(chunk)
        except Exception as e:
            logger.error(f"Error handling '{text}': {e}", exc_info=True)
            await channel.send(f"Error: {e}")

    async def _slash(self, interaction: discord.Interaction, command: str):
        """Common handler for all slash commands."""
        if not self._is_allowed(interaction.user.id):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        self._known_channel_ids.add(interaction.channel_id)
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        reply = await loop.run_in_executor(None, self.brain.handle, command, interaction.user.id)
        if not reply:
            reply = "(No response)"
        reply_md = _html_to_discord(reply)
        chunks = _chunk_text(reply_md)
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    def _register_commands(self):
        tree = self.tree

        @tree.command(name="list", description="Browse your library (optional: novel/anime, status)")
        @app_commands.describe(filter="e.g. novel, anime, reading, completed, watching")
        async def cmd_list(interaction: discord.Interaction, filter: str = ""):
            await self._slash(interaction, f"/list {filter}".strip())

        @tree.command(name="add", description="Track a new novel or anime")
        @app_commands.describe(what="e.g. novel Solo Leveling  or  anime Blue Lock")
        async def cmd_add(interaction: discord.Interaction, what: str):
            await self._slash(interaction, f"/add {what}")

        @tree.command(name="find", description="Search your library by title")
        @app_commands.describe(query="Title or keyword to search for")
        async def cmd_find(interaction: discord.Interaction, query: str):
            await self._slash(interaction, f"/find {query}")

        @tree.command(name="status", description="Update reading/watching status for an item")
        @app_commands.describe(id="Item number", status="reading, watching, on_hold, completed, dropped")
        async def cmd_status(interaction: discord.Interaction, id: int, status: str):
            await self._slash(interaction, f"/status {id} {status}")

        @tree.command(name="rate", description="Rate an item 0–10")
        @app_commands.describe(id="Item number", score="Score from 0 to 10", note="Optional note")
        async def cmd_rate(interaction: discord.Interaction, id: int, score: str, note: str = ""):
            await self._slash(interaction, f"/rate {id} {score} {note}".strip())

        @tree.command(name="progress", description="Update your chapter or episode progress")
        @app_commands.describe(id="Item number", current="Current chapter/episode", total="Total (optional)")
        async def cmd_progress(interaction: discord.Interaction, id: int, current: int, total: str = ""):
            rest = f"{id} {current}" + (f"/{total}" if total else "")
            await self._slash(interaction, f"/progress {rest}")

        @tree.command(name="note", description="Add or update a note on an item")
        @app_commands.describe(id="Item number", text="Your note")
        async def cmd_note(interaction: discord.Interaction, id: int, text: str):
            await self._slash(interaction, f"/note {id} {text}")

        @tree.command(name="tag", description="Add a tag to an item")
        @app_commands.describe(id="Item number", tag="Tag to add e.g. cultivation, isekai")
        async def cmd_tag(interaction: discord.Interaction, id: int, tag: str):
            await self._slash(interaction, f"/tag {id} {tag}")

        @tree.command(name="remove", description="Stop tracking an item")
        @app_commands.describe(id="Item number to remove")
        async def cmd_remove(interaction: discord.Interaction, id: int):
            await self._slash(interaction, f"/remove {id}")

        @tree.command(name="fix", description="Force-fix broken scrapers")
        @app_commands.describe(target="Item number, 'broken' to fix all, or 'clear <id>' to dismiss")
        async def cmd_fix(interaction: discord.Interaction, target: str = "broken"):
            await self._slash(interaction, f"/fix {target}")

        @tree.command(name="broken", description="List all items with broken scrapers")
        async def cmd_broken(interaction: discord.Interaction):
            await self._slash(interaction, "/broken")

        @tree.command(name="check", description="Force-check entire library for updates now")
        async def cmd_check(interaction: discord.Interaction):
            await self._slash(interaction, "/check")

        @tree.command(name="recent", description="Show items updated recently")
        @app_commands.describe(days="How many days back to look (default 7)")
        async def cmd_recent(interaction: discord.Interaction, days: int = 7):
            await self._slash(interaction, f"/recent {days}")

        @tree.command(name="stats", description="Library counts and summary stats")
        async def cmd_stats(interaction: discord.Interaction):
            await self._slash(interaction, "/stats")

        @tree.command(name="history", description="Show recent bot event log")
        async def cmd_history(interaction: discord.Interaction):
            await self._slash(interaction, "/history")

        @tree.command(name="health", description="Bot self-check — DB, scheduler, scraper status")
        async def cmd_health(interaction: discord.Interaction):
            await self._slash(interaction, "/health")

        @tree.command(name="ask", description="Ask anything in plain English — recommendations, questions, commands")
        @app_commands.describe(question="What do you want to know or do?")
        async def cmd_ask(interaction: discord.Interaction, question: str):
            await self._slash(interaction, f"/ask {question}")

        @tree.command(name="help", description="Show all available commands")
        async def cmd_help(interaction: discord.Interaction):
            await self._slash(interaction, "/help")

    def send_to_all_known(self, text: str):
        async def _send_all():
            text_md = _html_to_discord(text)
            chunks = _chunk_text(text_md)
            success = 0
            for channel_id in self._known_channel_ids:
                try:
                    ch = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
                    for chunk in chunks:
                        await ch.send(chunk)
                    success += 1
                except Exception as e:
                    logger.warning(f"Failed to notify Discord channel {channel_id}: {e}")
            logger.info(f"Notified {success}/{len(self._known_channel_ids)} Discord channels")

        if not self._known_channel_ids:
            return
        loop = getattr(self.client, "loop", None)
        if loop and loop.is_running():
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