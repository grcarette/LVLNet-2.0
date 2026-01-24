import discord
import os

MODE_TAGS = {
    "challenge": 1449441012169707673,
    "party": 1449440516923064422
}


class LevelHandler:
    def __init__(self, bot):
        self.bot = bot
        self.dh = self.bot.dh

        self.bot_logs_channel_id = int(os.getenv('BOT_LOGS_CHANNEL_ID'))
    
    async def set_tourney_legality(self, level_code, legality):
        legality_changed = await self.dh.set_tourney_legality(level_code, legality)
        if not legality_changed:
            print(legality_changed)
            return

        result = await self.bot.logh.log_legality(level_code, legality)
        return result

    async def post_level(self, imgur_url, mode, creators, post_to_forum=True):
        imgur_data = await self.bot.ih.get_imgur_data(imgur_url)
        if not imgur_data or not await self.verify_code(imgur_data['code']):
            return False

        if post_to_forum:
            creator_names = ", ".join(user.display_name for user in creators)
            creator_ids = [user.id for user in creators]
        else:
            creator_ids = creators

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
        if post_to_forum:
            await self.post_level_to_forum(level_data, creator_names, mode)
        return True

    async def post_level_to_forum(self, level_data, creator_names, mode):
        forum_channel_id = int(os.getenv('LEVEL_FORUM_CHANNEL_ID'))
        forum_channel = discord.utils.get(self.bot.guild.forums, id=forum_channel_id)

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

    async def remove_level(self, code, user):
        level = await self.dh.get_level(code)
        if not level:
            return False

        if user.id not in level['creators']:
            return False

        forum_channel_id = int(os.getenv('LEVEL_FORUM_CHANNEL_ID'))
        forum_channel = discord.utils.get(self.bot.guild.forums, id=forum_channel_id)

        thread = self.bot.get_channel(level['forum_post_id'])
        if thread:
            await thread.delete()
        await self.dh.remove_level(code)
        return True

    async def verify_code(self, code):
        print(code[4], code, len(code))
        if not code[4] == '-' or len(code) != 9:
            print('false!!')
            return False
        return True

