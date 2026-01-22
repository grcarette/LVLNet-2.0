import discord
import asyncio

class CocreatorSelectMenu(discord.ui.UserSelect):
    def __init__(self, secondary_callback):
        super().__init__()
        self.secondary_callback = secondary_callback
    
    async def callback(self, interaction: discord.Interaction):
        selected_user = self.values[0]
        await self.secondary_callback(selected_user)
        message = await interaction.response.send_message(f"Added {selected_user.mention}", ephemeral=True)
        message_obj = await interaction.original_response()

        asyncio.create_task(self.delete_after(message_obj, 1.5))

    async def delete_after(self, message, delay):
        await asyncio.sleep(delay)
        try: 
            await message.delete()
        except discord.NotFound:
            pass

class RemoveLevelModal(discord.ui.Modal, title="Remove Level"):
    code = discord.ui.TextInput(label="Level Code", placeholder="Enter the code of the level you wish to remove")

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    async def on_submit(self, interaction: discord.Interaction):
        await self.callback(interaction, self.code.value)

class LevelConfigModal(discord.ui.Modal, title="Post Level"):
    imgur = discord.ui.TextInput(label="Imgur Link", placeholder="Enter the Imgur link for the level")

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    async def on_submit(self, interaction: discord.Interaction):
        await self.callback(interaction, self.imgur.value)

class ModeSelectionView(discord.ui.View):
    def __init__(self, imgur_link, user, callback, timeout=300):
        super().__init__(timeout=timeout)
        self.imgur_link = imgur_link
        self.callback = callback
        self.creators = [user]
        self.difficulty = None

        self.add_cocreators_button = discord.ui.Button(
            label="Add Co-Creators",
            style=discord.ButtonStyle.primary
        )
        self.add_cocreators_button.callback = self.add_cocreators
        self.add_item(self.add_cocreators_button)

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

    async def add_cocreators(self, interaction: discord.Interaction):
        view = discord.ui.View()
        view.add_item(CocreatorSelectMenu(self.add_creator))
        await interaction.response.send_message(
            content="add cocreators:", 
            view=view, 
            ephemeral=True)

    async def add_creator(self, creator):
        self.creators.append(creator)

    async def toggle_party_mode(self, interaction: discord.Interaction):
        self.type = 'party'
        self.difficulty = None
        self.party_button.style = discord.ButtonStyle.primary
        self.challenge_button.style = discord.ButtonStyle.secondary
        self.submit_button.disabled = False
        await interaction.response.edit_message(view=self)
    
    async def toggle_challenge_mode(self, interaction: discord.Interaction):
        self.type = 'challenge'
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
        view = ModeSelectionView(imgur_link, interaction.user, self.handle_mode_selection)
        await interaction.response.send_message(
            content="Level Details:",
            view=view,
            ephemeral=True
        )

    async def handle_mode_selection(self, interaction: discord.Interaction, imgur_link: str, mode: str, creators: list):
        await interaction.response.defer(ephemeral=True)

        level_posted = await self.bot.lh.post_level(imgur_link, mode, creators)

        if level_posted:
            await interaction.delete_original_response()
        else:
            await interaction.followup.send(
                "Failed to post level: Either Level already exists or Imgur link is invalid.",
                ephemeral=True
            )

    async def remove_level(self, interaction: discord.Interaction):
        modal = RemoveLevelModal(self.handle_remove_level)
        await interaction.response.send_modal(modal)    

    async def handle_remove_level(self, interaction: discord.Interaction, code: str):
        await interaction.response.defer(ephemeral=True)

        level_removed = await self.bot.lh.remove_level(code, interaction.user)

        if level_removed:
            await interaction.followup.send(
                f"Level with code {code} has been removed.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Failed to remove level with code {code}: Level does not exist or you are not a creator.",
                ephemeral=True
            )
