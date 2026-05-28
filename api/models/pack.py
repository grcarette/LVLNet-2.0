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


class PackDraft(BaseModel):
    """A work-in-progress pack, stored in the separate `pack_drafts` collection.

    Deliberately permissive: `name` may be blank and `levels` may be empty or
    below the publish minimum while the author is still assembling it. The publish
    step (POST /packs/drafts/{id}/publish) enforces the real rules and moves the
    document into `packs`. There is no `deleted` flag — drafts are hard-deleted."""
    pack_id: str
    author: int
    name: str = ""
    description: str = ""
    thumbnail: Optional[Thumbnail] = None
    levels: List[str] = []  # mutable while a draft; frozen on publish
    created_at: datetime
    updated_at: datetime