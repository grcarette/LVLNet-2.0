import discord
import os

class LogHandler:
    def __init__(self, bot):
        self.bot = bot
        self.dh = self.bot.dh
        self.bot_logs_channel_id = int(os.getenv('BOT_LOGS_CHANNEL_ID'))

    async def log_legality(self, level_code, legality):
        level = await self.dh.get_level(level_code)
        log_channel = discord.utils.get(self.bot.guild.channels, id=self.bot_logs_channel_id)

        if legality:
            embed_color = discord.Color.green()
            message = "Legal"
        else:
            embed_color = discord.Color.red()
            message = "Illegal"
        embed = discord.Embed(
            title=f"Stage Legality updated",
            color=embed_color,
            description=f"Stage `{level['name']}` with code `{level['code']}` made {message}"
        )
        await log_channel.send(embed=embed)
        return True