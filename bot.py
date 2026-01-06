import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

from handlers.data_handler import DataHandler
from handlers.imgur_handler import ImgurHandler
from handlers.ui_handler import UIHandler

MODE_TAGS = {
    "Challenge": 1449441012169707673,
    "Party": 1449440516923064422
}

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

    async def post_level(self, imgur_link, mode, user_id):
        imgur_data = await self.ih.get_imgur_data(imgur_link)
        if not imgur_data:
            return False
        
        print(imgur_data)
        forum_channel_id = int(os.getenv('LEVEL_FORUM_CHANNEL_ID'))
        forum_channel = discord.utils.get(self.guild.forums, id=forum_channel_id)

        tag_map = {tag.id: tag for tag in forum_channel.available_tags}
        tag_id = MODE_TAGS[mode]
        forum_tag = tag_map[tag_id]

        title = f"{imgur_data['code']} - {imgur_data['title']} - by <@{user_id}>"
        content = imgur_data['url']
        post = await forum_channel.create_thread(
            name=title,
            content=content,
            applied_tags=[forum_tag]
        )
        return True

if __name__ == "__main__":
    load_dotenv()
    intents = discord.Intents.default()

    intents.members = True
    intents.messages = True 
    intents.message_content = True
    intents.guilds = True
    bot = LVLNetBot(command_prefix="!", intents=intents)
    bot.run(os.getenv("DISCORD_TOKEN"))