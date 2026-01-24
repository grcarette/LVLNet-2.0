import discord
import os

class LogHandler:
    def __init__(self, bot):
        self.bot = bot
        self.dh = self.bot.dh
        self.bot_logs_channel_id = int(os.getenv('BOT_LOGS_CHANNEL_ID'))

    async def get_log_channel(self):
        log_channel = discord.utils.get(self.bot.guild.channels, id=self.bot_logs_channel_id)
        return log_channel

    async def log_legality(self, level_code, legality):
        level = await self.dh.get_level(level_code)
        log_channel = await self.get_log_channel()

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

    async def log_user(self, username, discord_id):
        log_channel = await self.get_log_channel()
        embed_color = discord.Color.blue()
        embed = discord.Embed(
            title=f"Registered user",
            color= embed_color,
            description=f"Registered user {username} with id {discord_id}"
        )
        await log_channel.send(embed=embed)
        return True

    async def log_user_not_found(self, discord_id):
        log_channel = await self.get_log_channel()
        embed_color = discord.Color.red()
        embed = discord.Embed(
            title=f"User not found",
            color= embed_color,
            description=f"Could not find user with id:`{discord_id}`. Manually registration needed"
        )

