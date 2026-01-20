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

    @app_commands.command(
        name="r",
        description="Get a random tournament legal level"
    )
    @app_commands.describe(number="Number of levels to retrieve (max 4)")
    async def random_level(self, interaction: discord.Interaction, number: int = 1):
        await interaction.response.defer(ephemeral=False)

        levels = await self.bot.dh.get_random_levels(number, tournament_legal=True)
        
        if not levels:
            await interaction.followup.send("Error retrieving levels.")
            return

        embed_list = []

        for level in levels:
            creator_names = []
            for creator_id in level['creators']:
                member = interaction.guild.get_member(int(creator_id))
                if member:
                    creator_names.append(member.display_name)
                else:
                    try:
                        user = await self.bot.fetch_user(int(creator_id))
                        creator_names.append(user.display_name)
                    except:
                        creator_names.append(f"Unknown({creator_id})")
            creators_string = ", ".join(creator_names)
            embed = discord.Embed(
                title=level['name'],
                color=discord.Color.blue()
            )
            embed.add_field(name="Creator", value=creators_string, inline=True)
            embed.add_field(name="Code", value=f"`{level['code']}`", inline=True)
            embed.add_field(name="Mode", value=level['mode'].capitalize(), inline=True)
            
            if level.get('imgur_url'):
                raw_url = level['imgur_url'].split('?')[0].rstrip('/')
                split_index = max(raw_url.rfind('-'), raw_url.rfind('/'))
                image_id = raw_url[split_index + 1:].split('.')[0]
                direct_link = f"https://i.imgur.com/{image_id}.png"
                
                embed.set_image(url=direct_link)
                embed.description = f"[View on Imgur]({level['imgur_url']})"
            embed_list.append(embed)

        await interaction.followup.send(embeds=embed_list)

async def setup(bot):
    await bot.add_cog(LevelCog(bot))