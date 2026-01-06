import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

from handlers.data_handler import DataHandler
from handlers.imgur_handler import ImgurHandler
from handlers.ui_handler import UIHandler

class LVLNetBot(commands.Bot):
    def __init__(self, command_prefix, intents):
        super().__init__(command_prefix=command_prefix, intents=intents)

        self.dh = DataHandler(self)
        self.ih = ImgurHandler(self)
        self.uh = UIHandler(self)

        self.level_sharing_channel_id = int(os.getenv('LEVEL_SHARING_CHANNEL_ID'))

    async def on_ready(self):
        self.guild = self.guilds[0]
        print("Bot initialized")

        level_sharing_channel_id = int(os.getenv('LEVEL_SHARING_CHANNEL_ID'))
        await self.uh.initialize(level_sharing_channel_id)

if __name__ == "__main__":
    load_dotenv()
    intents = discord.Intents.default()

    intents.members = True
    intents.messages = True 
    intents.message_content = True
    intents.guilds = True
    bot = LVLNetBot(command_prefix="!", intents=intents)
    bot.run(os.getenv("DISCORD_TOKEN"))