import os
import logging

import aiohttp

logger = logging.getLogger("lvlnet.imgur")


def _parse_image_id(imgur_url: str) -> str | None:
    clean_url = imgur_url.split("?")[0].rstrip("/")
    split_index = max(clean_url.rfind("-"), clean_url.rfind("/"))
    image_id = clean_url[split_index + 1:].split(".")[0]
    if not image_id or len(image_id) < 5:
        return None
    return image_id


async def get_imgur_data(imgur_url: str) -> dict | None:
    """Resolve an Imgur album/image URL to {title, code, image_url}.
    Returns None if the URL can't be parsed or Imgur has no record of it."""
    image_id = _parse_image_id(imgur_url)
    if not image_id:
        logger.error("Failed to parse ID from: %s", imgur_url)
        return None

    headers = {"Authorization": f"Client-ID {os.getenv('IMGUR_CLIENT_ID')}"}
    endpoints = [
        f"https://api.imgur.com/3/gallery/album/{image_id}",
        f"https://api.imgur.com/3/image/{image_id}",
    ]

    async with aiohttp.ClientSession(headers=headers) as session:
        for url in endpoints:
            async with session.get(url) as response:
                if response.status != 200:
                    continue
                data = (await response.json()).get("data", {})
                title = data.get("title") or "Untitled"
                code = data.get("description") or ""
                if not code and "images" in data:
                    code = data["images"][0].get("description") or ""
                image_url = data.get("link")
                if not image_url and "images" in data:
                    image_url = data["images"][0].get("link")
                return {"title": title, "code": code.strip(), "image_url": image_url}

    logger.error("Imgur API lookup failed for ID %s", image_id)
    return None