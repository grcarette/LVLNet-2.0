class UIHandler:
    def __init__(self, bot):
        self.bot = bot

    async def initialize(self, channel):
        
        await channel.send(embed=embed, view=view)