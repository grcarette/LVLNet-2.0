from pydantic import BaseModel
from typing import List

class Level(BaseModel):
    code: str
    name: str
    imgur_url: str
    creators: List[int]
    tags: List[str]
    tournament_legal: bool = False

class LevelCreateRequest(BaseModel):
    imgur_url: str
    mode: str
    creators: List[int]   # discord IDs
    hidden: bool = False