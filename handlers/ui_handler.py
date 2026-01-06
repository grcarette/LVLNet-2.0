from ui.level_sharing import LevelSharingView
import discord

class UIHandler:
    def __init__(self, bot):
        self.bot = bot

    async def initialize(self, level_sharing_channel_id):
        channel = discord.utils.get(self.bot.guild.text_channels, id=level_sharing_channel_id)
        if channel:
            view = LevelSharingView(self.bot)
            await channel.send(view=view)