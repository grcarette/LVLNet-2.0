import discord

class LevelConfigModal(discord.ui.Modal, title="Post Level"):
    imgur = discord.ui.TextInput(label="Imgur Link", placeholder="Enter the Imgur link for the level")

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    async def on_submit(self, interaction: discord.Interaction):
        await self.callback(interaction, self.imgur.value)


class ModeSelectionView(discord.ui.View):
    def __init__(self, imgur_link, user_id, callback, timeout=300):
        super().__init__(timeout=timeout)
        self.imgur_link = imgur_link
        self.callback = callback
        self.creators = [user_id]

        self.party_button = discord.ui.Button(
            label="Party Mode", 
            style=discord.ButtonStyle.secondary
            )
        self.party_button.callback = self.toggle_party_mode
        self.add_item(self.party_button)
        self.challenge_button = discord.ui.Button(
            label="Challenge Mode", 
            style=discord.ButtonStyle.secondary
            )
        self.challenge_button.callback = self.toggle_challenge_mode
        self.add_item(self.challenge_button)
        self.submit_button = discord.ui.Button(
            label="Submit", 
            style=discord.ButtonStyle.success, 
            disabled=True
            )
        self.submit_button.callback = self.submit_level
        self.add_item(self.submit_button)

    async def toggle_party_mode(self, interaction: discord.Interaction):
        self.type = 'Party'
        self.party_button.style = discord.ButtonStyle.primary
        self.challenge_button.style = discord.ButtonStyle.secondary
        self.submit_button.disabled = False
        await interaction.response.edit_message(view=self)
    
    async def toggle_challenge_mode(self, interaction: discord.Interaction):
        self.type = 'Challenge'
        self.challenge_button.style = discord.ButtonStyle.primary
        self.party_button.style = discord.ButtonStyle.secondary
        self.submit_button.disabled = False
        await interaction.response.edit_message(view=self)

    async def submit_level(self, interaction: discord.Interaction):
        self.submit_button.disabled = True
        await self.callback(interaction, self.imgur_link, self.type, self.creators)

class LevelSharingView(discord.ui.View):
    def __init__(self, bot, timeout=None):
        super().__init__(timeout=timeout)
        self.bot = bot

        self.post_button = discord.ui.Button(
            label="Post Level",
            style=discord.ButtonStyle.primary,
            custom_id="post-level"
        )
        self.post_button.callback = self.post_level
        self.add_item(self.post_button)

        self.remove_post_button = discord.ui.Button(
            label="Remove Level",
            style=discord.ButtonStyle.danger,
            custom_id="remove-level"
        )
        self.remove_post_button.callback = self.remove_level
        self.add_item(self.remove_post_button)

    async def post_level(self, interaction: discord.Interaction):
        modal = LevelConfigModal(self.handle_modal_submission)
        await interaction.response.send_modal(modal)

    async def handle_modal_submission(self, interaction: discord.Interaction, imgur_link: str):
        view = ModeSelectionView(imgur_link, interaction.user.id, self.handle_mode_selection)
        await interaction.response.send_message(
            content="Select the mode for your level:",
            view=view,
            ephemeral=True
        )

    async def handle_mode_selection(self, interaction: discord.Interaction, imgur_link: str, mode: str, creators: list):
        await interaction.response.defer(ephemeral=True)

        level_posted = await self.bot.post_level(imgur_link, mode, creators)

        if level_posted:
            await interaction.followup.send(
                f"Level posted successfully",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Failed to post level. Please try again later.",
                ephemeral=True
            )

    async def remove_level(self, interaction: discord.Interaction):
        await interaction.response.send_message("Remove level not implemented yet.", ephemeral=True)


