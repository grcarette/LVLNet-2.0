from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional

# Campaign and Speedrun are SEPARATE leaderboards (open question #1).
VALID_MODES = ("Campaign", "Speedrun")
# An omitted `mode` on the read path resolves to the primary competitive board.
DEFAULT_MODE = "Speedrun"


class TimeSubmission(BaseModel):
    """Body of `POST /packs/{packId}/times`.

    The Unity client serializes this with `JsonUtility`, i.e. camelCase keys.
    Aliases map the wire format onto snake_case attributes; `populate_by_name`
    lets tests/other callers construct it with either spelling. `extra="ignore"`
    keeps us liberal in what we accept (brief: "Be liberal ... on submit").
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    pack_id: str = Field(default="", alias="packId")
    version: int = 1
    gsid: str
    platform_id: str = Field(default="", alias="platformId")
    display_name: str = Field(default="", alias="displayName")
    mode: str = DEFAULT_MODE
    total_seconds: float = Field(alias="totalSeconds")
    splits: List[float] = Field(default_factory=list)

    # Reserved for a future Steam session-ticket verification step (Trust model).
    # Accepted but never required or acted upon in v1.
    ticket: Optional[str] = None


def normalize_mode(value: Optional[str]) -> Optional[str]:
    """Return the canonical mode ("Campaign"/"Speedrun") for a case-insensitive
    input, or None if it is not a recognised mode."""
    if not value:
        return None
    lowered = value.strip().lower()
    for canonical in VALID_MODES:
        if canonical.lower() == lowered:
            return canonical
    return None