from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


class Thumbnail(BaseModel):
    data: bytes
    content_type: str


class Pack(BaseModel):
    pack_id: str
    author: int            # discord_id, resolved to username at the API layer
    name: str
    description: str = ""
    thumbnail: Optional[Thumbnail] = None
    levels: List[str] = []  # FINAL: set at creation, never changed afterward
    deleted: bool = False
    created_at: datetime
    updated_at: datetime