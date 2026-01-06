import motor.motor_asyncio

class DataHandler:
    def __init__(self, bot, db_name="LVLNet2"):
        self.bot = bot
        
        self.client = motor.motor_asyncio.AsyncIOMotorClient("mongodb://localhost:27017")
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

