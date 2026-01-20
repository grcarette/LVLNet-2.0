import motor.motor_asyncio
import os  

class DataHandler:
    def __init__(self, bot, db_name="LVLNet2"):
        self.bot = bot
        
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        self.client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self.db = self.client[db_name]
        self.level_collection = self.db['levels']

    async def add_level(self, level_data):
        query = {
            'code': level_data['code']
        }
        level = await self.level_collection.find_one(query)
        if level:
            return False 

        result = await self.level_collection.insert_one(level_data)
        return result.inserted_id

    async def remove_level(self, level_code):
        result = await self.level_collection.delete_one({'code': level_code})
        return result.deleted_count

    async def get_level(self, level_code):
        level = await self.level_collection.find_one({'code': level_code})
        return level

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
                'mode': 'Party'
            },
            {
                '$set': {'tournament_legal': is_legal}
            }
        )
        return result.modified_count


