import os
import aiohttp
import discord

# Tag IDs from the ORIGINAL server's forum. Used only as a fallback when a
# forum's tags can't be matched by name (see _resolve_forum_tag).
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

    def _resolve_forum_tag(self, forum_channel, mode):
        """Pick the forum tag(s) to apply for this mode.

        Prefers a tag whose NAME matches the mode, so a newly-joined server only
        needs tags named 'party' / 'challenge'. Falls back to the original
        server's known tag IDs (MODE_TAGS) so the main forum keeps tagging
        exactly as before. If neither is present in this forum, the post is
        created untagged rather than failing."""
        tag = discord.utils.find(
            lambda t: t.name.lower() == mode.lower(),
            forum_channel.available_tags,
        )
        if tag is not None:
            return [tag]
        tag_id = MODE_TAGS.get(mode)
        if tag_id is not None:
            by_id = discord.utils.get(forum_channel.available_tags, id=tag_id)
            if by_id is not None:
                return [by_id]
        return []

    def _find_forum(self, guild):
        """Resolve which forum channel to post into for a given guild.

        The main server is matched by its configured forum ID, so its behaviour
        is identical to the single-server setup and never regresses (channel IDs
        are globally unique, so this only ever matches the main forum). Every
        other server is matched by forum NAME (LEVEL_FORUM_NAME, default
        'level-sharing'); a newly-joined server just needs a Forum channel with
        that name."""
        main_forum_id = int(os.getenv('LEVEL_FORUM_CHANNEL_ID'))
        forum = discord.utils.get(guild.forums, id=main_forum_id)
        if forum is not None:
            return forum
        forum_name = os.getenv('LEVEL_FORUM_NAME', 'level-sharing')
        return discord.utils.get(guild.forums, name=forum_name)

    async def post_level_to_forum(self, level_data, creator_names, mode):
        """Create the forum thread in EVERY server the bot is in that has a
        matching forum, and record each created thread so removal can later
        clean them all up. A failure in one server is logged and skipped so it
        can't abort posting to the others."""
        title = f"{level_data['code']} - {level_data['name']} - by {creator_names}"
        posts = []
        for guild in self.bot.guilds:
            forum_channel = self._find_forum(guild)
            if forum_channel is None:
                continue  # no matching forum in this server; skip it
            applied_tags = self._resolve_forum_tag(forum_channel, mode)
            try:
                post = await forum_channel.create_thread(
                    name=title,
                    content=level_data['imgur_url'],
                    applied_tags=applied_tags,
                )
            except discord.HTTPException as e:
                print(f"[forum mirror] failed in guild {guild.id}: {e}")
                continue
            posts.append({
                "guild_id": guild.id,
                "channel_id": forum_channel.id,
                "thread_id": post.thread.id,
            })
        await self.dh.attach_posts_to_level(level_data['code'], posts)

    async def remove_level(self, code, user):
        level = await self.dh.get_level(code)
        if not level:
            return False

        is_creator = user.id in level['creators']
        is_event_organizer = any(role.name == "Event Organizer" for role in user.roles)
        is_hidden = level.get('hidden', False)

        if not is_creator and not (is_event_organizer and is_hidden):
            return False

        # Delete every mirrored forum thread. Levels created before mirroring
        # stored a single 'forum_post_id'; handle both shapes for safety.
        posts = level.get('forum_posts')
        if posts is None:
            legacy_id = level.get('forum_post_id')
            posts = [{"thread_id": legacy_id}] if legacy_id else []
        for entry in posts:
            thread = self.bot.get_channel(entry.get("thread_id"))
            if thread is not None:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass

        await self.dh.remove_level(code)
        return True