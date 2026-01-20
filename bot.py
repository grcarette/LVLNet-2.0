import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

from handlers.data_handler import DataHandler
from handlers.imgur_handler import ImgurHandler
from handlers.ui_handler import UIHandler
from handlers.reaction_handler import ReactionHandler

MODE_TAGS = {
    "challenge": 1449441012169707673,
    "party": 1449440516923064422
}

class LVLNetBot(commands.Bot):
    def __init__(self, command_prefix, intents):
        super().__init__(command_prefix=command_prefix, intents=intents)

        self.dh = DataHandler(self)
        self.ih = ImgurHandler(self)
        self.uh = UIHandler(self)
        self.rh = ReactionHandler(self)

        self.level_sharing_channel_id = int(os.getenv('LEVEL_SHARING_CHANNEL_ID'))

    async def setup_hook(self):
        await self.load_extension("cogs.level_cog")
        GUILD = discord.Object(id=int(os.getenv('GUILD_ID')))
        self.tree.copy_global_to(guild=GUILD)
        await self.tree.sync(guild=GUILD)

    async def on_ready(self):
        self.guild = self.guilds[0]
        print("Bot initialized")

        level_sharing_channel_id = int(os.getenv('LEVEL_SHARING_CHANNEL_ID'))
        await self.uh.initialize(level_sharing_channel_id)

    async def verify_code(self, code):
        print(code[4], code, len(code))
        if not code[4] == '-' or len(code) != 9:
            print('false!!')
            return False
        return True

    async def post_level(self, imgur_url, mode, creators):
        imgur_data = await self.ih.get_imgur_data(imgur_url)
        if not imgur_data or not await self.verify_code(imgur_data['code']):
            return False

        creator_names = ", ".join(user.display_name for user in creators)
        creator_ids = [user.id for user in creators]

        level_data = {
            'imgur_url': imgur_url,
            'name': imgur_data['title'],
            'code': imgur_data['code'],
            'mode': mode,
            'creators': creator_ids,
            'tournament_legal': False
        }

        level_added = await self.dh.add_level(level_data)
        if not level_added:
            return False
        
        forum_channel_id = int(os.getenv('LEVEL_FORUM_CHANNEL_ID'))
        forum_channel = discord.utils.get(self.guild.forums, id=forum_channel_id)

        tag_map = {tag.id: tag for tag in forum_channel.available_tags}
        tag_id = MODE_TAGS[mode]
        forum_tag = tag_map[tag_id]

        title = f"{level_data['code']} - {level_data['name']} - by {creator_names}"
        content = level_data['imgur_url']
        post = await forum_channel.create_thread(
            name=title,
            content=content,
            applied_tags=[forum_tag]
        )

        await self.dh.attach_post_to_level(level_data['code'], post.thread.id)

        return True

    async def remove_level(self, code, user):
        level = await self.dh.get_level(code)
        if not level:
            return False

        if user.id not in level['creators']:
            return False

        forum_channel_id = int(os.getenv('LEVEL_FORUM_CHANNEL_ID'))
        forum_channel = discord.utils.get(self.guild.forums, id=forum_channel_id)

        thread = self.get_channel(level['forum_post_id'])
        if thread:
            await thread.delete()
        await self.dh.remove_level(code)
        return True

if __name__ == "__main__":
    load_dotenv()
    intents = discord.Intents.default()

    intents.members = True
    intents.messages = True 
    intents.message_content = True
    intents.guilds = True
    intents.reactions = True

    bot = LVLNetBot(command_prefix="/", intents=intents)
    bot.run(os.getenv("DISCORD_TOKEN"))