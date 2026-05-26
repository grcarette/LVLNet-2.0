import os
import aiohttp
import discord

MODE_TAGS = {
    "challenge": 1449441012169707673,
    "party": 1449440516923064422,
}


class LevelHandler:
    def __init__(self, bot):
        self.bot = bot
        self.dh = self.bot.dh

        self.bot_logs_channel_id = int(os.getenv('BOT_LOGS_CHANNEL_ID'))
        self.api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
        self.api_key = os.getenv("PACKS_API_KEY")

    async def set_tourney_legality(self, level_code, legality):
        legality_changed = await self.dh.set_tourney_legality(level_code, legality)
        if not legality_changed:
            print(legality_changed)
            return
        return

    async def post_level(self, imgur_url, mode, creators, post_to_forum=True, hidden=False):
        creator_ids = [c.id if hasattr(c, "id") else int(c) for c in creators]
        creator_names = ", ".join(
            c.display_name for c in creators if hasattr(c, "display_name")
        )

        result, error = await self._upload_via_api(imgur_url, mode, creator_ids, hidden)
        if result is None:
            return None, error

        for cid in creator_ids:
            await self.dh.get_username(cid)

        is_new = result.get("created") or result.get("unhidden")
        if post_to_forum and is_new and not result.get("hidden"):
            level_data = {
                "code": result["code"],
                "imgur_url": result["imgur_url"],
                "name": result["name"],
                "creators": creator_ids,
                "mode": result["mode"],
            }
            await self.post_level_to_forum(level_data, creator_names, mode)

        return result, None

    async def _upload_via_api(self, imgur_url, mode, creator_ids, hidden):
        """POST the level to the API.
        Returns (data, None) on success or (None, message) on any failure."""
        payload = {
            "imgur_url": imgur_url,
            "mode": mode,
            "creators": creator_ids,
            "hidden": hidden,
        }
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.api_base_url}/levels/", json=payload, headers=headers
                ) as resp:
                    if resp.status in (200, 201):
                        return await resp.json(), None

                    detail = None
                    try:
                        detail = (await resp.json()).get("detail")
                    except Exception:
                        pass

                    if resp.status in (401, 403, 503):
                        # Config/auth problem — operator's fault, not the user's.
                        print(f"[level upload] auth/config error {resp.status}: {detail}")
                        return None, "Upload service is misconfigured. Contact an admin."
                    if resp.status == 409:
                        return None, detail or "A level with that code already exists."
                    if resp.status == 400:
                        return None, detail or "Invalid Imgur link or level code."
                    print(f"[level upload] unexpected status {resp.status}: {detail}")
                    return None, "Upload failed due to an unexpected error."
        except aiohttp.ClientError as e:
            print(f"[level upload] network error: {e}")
            return None, "Upload service is unavailable. Try again later."

    async def post_level_to_forum(self, level_data, creator_names, mode):
        forum_channel_id = int(os.getenv('LEVEL_FORUM_CHANNEL_ID'))
        forum_channel = discord.utils.get(self.bot.guild.forums, id=forum_channel_id)

        tag_map = {tag.id: tag for tag in forum_channel.available_tags}
        forum_tag = tag_map[MODE_TAGS[mode]]

        title = f"{level_data['code']} - {level_data['name']} - by {creator_names}"
        post = await forum_channel.create_thread(
            name=title,
            content=level_data['imgur_url'],
            applied_tags=[forum_tag],
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