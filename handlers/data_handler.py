import motor.motor_asyncio
import os  

class DataHandler:
    def __init__(self, bot, db_name="LVLNet2"):
        self.bot = bot
        
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        self.client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self.db = self.client[db_name]
        self.level_collection = self.db['levels']
        self.user_collection = self.db['users']

    async def add_level(self, level_data):
        query = {
            'code': level_data['code']
        }
        level = await self.level_collection.find_one(query)
        if level:
            return False 

        for user_id in level_data['creators']:
            username = await self.get_username(user_id)
            await self.register_user(user_id, username)

        result = await self.level_collection.insert_one(level_data)
        return result.inserted_id

    async def remove_level(self, level_code):
        result = await self.level_collection.delete_one({'code': level_code})
        return result.deleted_count

    async def get_level(self, level_code):
        level = await self.level_collection.find_one({'code': level_code})
        return level

    async def get_random_levels(self, number=1, tournament_legal=True):
        query = {}
        if tournament_legal:
            query['tournament_legal'] = True

        if number > 4:
            number = 4

        pipeline = [
            {'$match': query},
            {'$sample': {'size': number}}
        ]
        levels = []
        async for level in self.level_collection.aggregate(pipeline):
            levels.append(level)
        return levels

    async def attach_post_to_level(self, level_code, post_id):
        result = await self.level_collection.update_one(
            {'code': level_code},
            {'$set': {'forum_post_id': post_id}}
        )
        return result.modified_count

    async def set_tourney_legality(self, level_code, is_legal):
        result = await self.level_collection.update_one(
            {
                'code': level_code,
                'mode': 'party'
            },
            {
                '$set': {'tournament_legal': is_legal}
            }
        )
        await self.bot.logh.log_legality(level_code, is_legal)
        return result.modified_count

    async def register_user(self, discord_id, username):
        query = {
            'discord_id': discord_id
        }
        user = await self.user_collection.find_one(query)
        if user:
            return

        data = {
            'discord_id': discord_id,
            'username': username
        }
        result = await self.user_collection.insert_one(data)
        await self.bot.logh.log_user(username, discord_id)
        return result.inserted_id

    async def get_username(self, discord_id):
        query = {
            'discord_id': discord_id
        }
        user_data = await self.user_collection.find_one(query)

        if user_data:
            return user_data['username']

        try:
            user = await self.bot.fetch_user(int(discord_id))
            username = user.display_name

            result = await self.register_user(discord_id, username)
            
            return username
        except Exception as e:
            await self.bot.logh.log_user_not_found(discord_id)
            return 'Unknown User'

    async def register_all_users(self):
        all_creator_ids = await self.db.levels.distinct("creators")
        existing_users = await self.db.users.distinct("discord_id")
        existing_set = set(existing_users)

        new_ids = [uid for uid in all_creator_ids if uid not in existing_set]

        for discord_id in new_ids:
            username = await self.get_username(discord_id)
            if username:
                await self.register_user(discord_id, username)
        



