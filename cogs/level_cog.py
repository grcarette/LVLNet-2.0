import discord
from discord.ext import commands
from discord import app_commands

class LevelCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="add_level",
        description="Add a party level directly to the database"
    )
    @app_commands.describe(imgur_link="Imgur link", creator_id="Creator ID")
    @app_commands.checks.has_any_role("Level Arbiter", "Event Organizer", "Moderator")
    async def add_level(self, interaction: discord.Interaction, imgur_link: str, creator_id: str):
        await interaction.response.defer(ephemeral=True)

        if not creator_id.isdigit():
            await interaction.followup.send(f"Error: invalid creator ID")
            return
        creator_id = int(creator_id)
        creator = discord.utils.get(self.bot.guild.members)

        level = await self.bot.ih.get_imgur_data(imgur_link)
        if not level:
            await interaction.followup.send(f"Error: invalid imgur link")
            return

        mode = "party"
        post_to_forum = False
        level_posted = await self.bot.lh.post_level(imgur_link, mode, [creator_id], post_to_forum)

        if not level_posted:
            await interaction.followup.send(f"Error: Failed to post level (perhaps it already exists?)")
        await interaction.followup.send(f"Level {level['title']} successfully added to database")

        await self.bot.lh.set_tourney_legality(level['code'], legality=True)
        return

    @app_commands.command(
        name="legality",
        description="Toggle the tournament legality of a level via its code"
    )
    @app_commands.describe(level_code="Code")
    @app_commands.checks.has_role("Level Arbiter")
    async def legality(self, interaction: discord.Interaction, level_code: str):
        await interaction.response.defer(ephemeral=True)

        level = await self.bot.dh.get_level(level_code)
        if not level:
            await interaction.followup.send(f"Level with code `{level_code}` not found.")
            return

        legality = not level.get('tournament_legal', False)
        success = await self.bot.lh.set_tourney_legality(level_code, legality)

        if not success:
            status_text = "LEGAL" if legality else "ILLEGAL"
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
                username = await self.bot.dh.get_username(creator_id)
                creator_names.append(username)
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