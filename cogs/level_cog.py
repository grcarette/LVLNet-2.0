import discord
from discord.ext import commands
from discord import app_commands

class LevelCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="legality",
        description="Toggle the tournament legality of a level via its code"
    )
    @app_commands.describe(level_code="Code:")
    @app_commands.checks.has_role("Level Arbiter")
    async def legality(self, interaction: discord.Interaction, level_code: str):
        await interaction.response.defer(ephemeral=True)

        level = await self.bot.dh.get_level(level_code)
        if not level:
            await interaction.followup.send(f"Level with code `{level_code}` not found.")
            return

        new_status = not level.get('tournament_legal', False)
        success = await self.bot.dh.set_tourney_legality(level_code, new_status)

        if success:
            status_text = "LEGAL" if new_status else "ILLEGAL"
            await interaction.followup.send(f"Level `{level_code}` is now marked as **{status_text}**.")
        else:
            await interaction.followup.send("Failed to update level legality.")

async def setup(bot):
    await bot.add_cog(LevelCog(bot))