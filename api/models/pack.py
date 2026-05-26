from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


class Thumbnail(BaseModel):
    data: bytes
    content_type: str

class PackVersion(BaseModel):
    version: int
    name: str
    description: str = ""
    thumbnail: Optional[Thumbnail] = None   # was: thumbnail_url: Optional[str]
    levels: List[str] = []
    created_at: datetime


class Pack(BaseModel):
    pack_id: str
    author: int            # discord_id, resolved to username at the API layer
    latest_version: int
    deleted: bool = False
    created_at: datetime
    updated_at: datetime
    versions: List[PackVersion]