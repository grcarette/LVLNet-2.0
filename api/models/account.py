from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class Account(BaseModel):
    """Player identity, keyed on GSID (spec §1.3).

    Auto-created on first contact (first vote or first gsid-bearing list call).
    `discord_id` is nullable and reserved for a later pairing phase that is out
    of scope for now (spec §8).
    """

    gsid: str
    discord_id: Optional[int] = None
    created_at: datetime