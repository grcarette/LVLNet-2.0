import os
import discord

class ReactionHandler:
    def __init__(self, bot):
        self.bot = bot
        self.dh = self.bot.dh

        self.allowed_channel_id = int(os.getenv('LEVEL_FORUM_CHANNEL_ID'))
        self.arbiter_role_name = "Level Arbiter"
        self.target_emoji = "✅"

        self.bot.add_listener(self.on_reaction_add)
        self.bot.add_listener(self.on_reaction_remove)

    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        await self.handle_reaction_change(reaction, user, set_legal=True)

    async def on_reaction_remove(self, reaction: discord.Reaction, user: discord.User):
        await self.handle_reaction_change(reaction, user, set_legal=False)

    async def handle_reaction_change(self, reaction: discord.Reaction, user: discord.User, set_legal: bool):
        if user.bot:
            return

        channel = reaction.message.channel
        forum_id = channel.id if not getattr(channel, 'parent', None) else channel.parent.id
        if forum_id != self.allowed_channel_id:
            return  
        
        guild = reaction.message.guild
        member = guild.get_member(user.id)
        arbiter_role = discord.utils.get(guild.roles, name=self.arbiter_role_name)
        if arbiter_role not in member.roles:
            return
    
        if str(reaction.emoji) != self.target_emoji:
            return

        level_code = self.extract_level_code(reaction.message)
        if not level_code:
            return
        
        await self.dh.set_tourney_legality(level_code, set_legal)

    def extract_level_code(self, message: discord.Message) -> str:
        if isinstance(message.channel, discord.Thread):
            title = message.channel.name
            return title[:9] 
        return None
