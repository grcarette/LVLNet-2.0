from pydantic import BaseModel, Field
from datetime import datetime


class VoteRequest(BaseModel):
    """Body of `POST /packs/{packId}/vote` (spec §4.1).

    `value` is validated in the route so an out-of-range value yields a clean
    400 rather than a 422; 1/-1 upsert the rating, 0 retracts (deletes) it.
    """

    gsid: str
    value: int = Field(description="1 (up), -1 (down), or 0 (retract)")


class Rating(BaseModel):
    """Source-of-truth rating record: exactly one per (pack_id, gsid).

    "No vote" is the *absence* of the record, never a stored 0 (spec §1.1).
    """

    pack_id: str
    gsid: str
    value: int  # 1 or -1 only; 0 is represented by the record not existing
    updated_at: datetime