import os
import discord

class ReactionHandler:
    def __init__(self, bot):
        self.bot = bot
        self.dh = self.bot.dh

        self.allowed_channel_id = int(os.getenv('LEVEL_FORUM_CHANNEL_ID'))
        self.arbiter_role_name = "Level Arbiter"
        self.target_emoji = "✅"

        # Raw events rather than on_reaction_add/remove: those only fire for
        # messages in the cache, and reaction_remove is only dispatched when the
        # user is in the member cache — which needs the privileged members intent.
        self.bot.add_listener(self.on_raw_reaction_add)
        self.bot.add_listener(self.on_raw_reaction_remove)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self.handle_reaction_change(payload, set_legal=True)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self.handle_reaction_change(payload, set_legal=False)

    async def handle_reaction_change(self, payload: discord.RawReactionActionEvent, set_legal: bool):
        if payload.guild_id is None or str(payload.emoji) != self.target_emoji:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = guild.get_channel_or_thread(payload.channel_id)
        if channel is None:
            # Archived forum posts aren't cached
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.Thread) or channel.parent_id != self.allowed_channel_id:
            return

        # Adds carry the member (with roles) in the payload; removes don't, so
        # fetch it — fetch_member is a plain API call and needs no intent.
        member = payload.member
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return
        if member.bot:
            return

        arbiter_role = discord.utils.get(guild.roles, name=self.arbiter_role_name)
        if arbiter_role not in member.roles:
            return

        level_code = self.extract_level_code(channel)
        if not level_code:
            return

        await self.bot.lh.set_tourney_legality(level_code, set_legal)

    def extract_level_code(self, thread: discord.Thread) -> str:
        return thread.name[:9]
