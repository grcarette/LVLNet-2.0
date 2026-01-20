
import os
import aiohttp
import logging

logger = logging.getLogger('discord')

class ImgurHandler:
    def __init__(self, bot):
        self.client_id = os.getenv("IMGUR_CLIENT_ID")
        self.headers = {
            "Authorization": f"Client-ID {self.client_id}"
        }

    async def get_imgur_data(self, imgur_url):
        clean_url = imgur_url.split('?')[0].rstrip('/')
        split_index = max(clean_url.rfind('-'), clean_url.rfind('/'))
        image_id = clean_url[split_index + 1:].split('.')[0]

        if not image_id or len(image_id) < 5:
            logger.error(f"Failed to parse ID from: {imgur_url}")
            return None

        endpoints = [
            f"https://api.imgur.com/3/gallery/album/{image_id}",
            f"https://api.imgur.com/3/image/{image_id}"
        ]

        async with aiohttp.ClientSession(headers=self.headers) as session:
            for url in endpoints:
                async with session.get(url) as response:
                    if response.status == 200:
                        json_resp = await response.json()
                        data = json_resp.get('data', {})
                        title = data.get('title') or "Untitled"
                        level_code = data.get('description') or ""
                        
                        if not level_code and 'images' in data:
                            level_code = data['images'][0].get('description') or ""
                        image_url = data.get('link')
                        if not image_url and 'images' in data:
                            image_url = data['images'][0].get('link')
                            
                        return {
                            "title": title,
                            "code": level_code.strip(),
                            "image_url": image_url
                        }
            
            logger.error(f"Imgur API Error {response.status} for ID {image_id}")
            return None

if __name__ == "__main__":
    import asyncio

    async def main():
        ih = ImgurHandler()
        data = await ih.get_imgur_data("https://imgur.com/7eq01Zj")
        print(data)

    asyncio.run(main())