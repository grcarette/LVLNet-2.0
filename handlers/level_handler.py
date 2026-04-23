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
        return

    async def post_level(self, imgur_url, mode, creators, post_to_forum=True, hidden=False):
        imgur_data = await self.bot.ih.get_imgur_data(imgur_url)
        if not imgur_data or not await self.verify_code(imgur_data['code']):
            return False

        code = imgur_data['code']

        # Normalize: creators may be Member objects (from UI) or raw IDs (from admin cog)
        creator_ids = [c.id if hasattr(c, 'id') else c for c in creators]
        creator_names = ", ".join(
            c.display_name for c in creators if hasattr(c, 'display_name')
        )

        existing = await self.dh.get_level(code)

        if existing:
            # Un-hide path: existing is hidden, new upload isn't, uploader is in stored creators
            if (
                existing.get('hidden')
                and not hidden
                and any(cid in existing['creators'] for cid in creator_ids)
            ):
                update_data = {
                    'imgur_url': imgur_url,
                    'name': imgur_data['title'],
                    'creators': creator_ids,
                    'mode': mode,
                    'hidden': False,
                }
                await self.dh.update_level(code, update_data)

                if post_to_forum:
                    level_data = {
                        'code': code,
                        'imgur_url': imgur_url,
                        'name': imgur_data['title'],
                        'creators': creator_ids,
                        'mode': mode,
                    }
                    await self.post_level_to_forum(level_data, creator_names, mode)
                return True

            # Duplicate of a visible level, or hidden-upload attempt of an existing level
            return False

        level_data = {
            'imgur_url': imgur_url,
            'name': imgur_data['title'],
            'code': code,
            'mode': mode,
            'creators': creator_ids,
            'tournament_legal': False,
            'hidden': hidden,
        }

        level_added = await self.dh.add_level(level_data)
        if not level_added:
            return False

        if post_to_forum and not hidden:
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

        is_creator = user.id in level['creators']
        is_event_organizer = any(role.name == "Event Organizer" for role in user.roles)
        is_hidden = level.get('hidden', False)

        if not is_creator and not (is_event_organizer and is_hidden):
            return False

        forum_post_id = level.get('forum_post_id')
        if forum_post_id:
            thread = self.bot.get_channel(forum_post_id)
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

