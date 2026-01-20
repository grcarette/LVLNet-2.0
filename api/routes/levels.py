from fastapi import APIRouter, HTTPException
from ..db import db
from api.models.level import Level

router = APIRouter(prefix="/levels", tags=["levels"])

@router.get("/{code}")
async def get_level(code: str):
    level = await db.levels.find_one({"code": code}, {"_id": 0})
    if not level:
        raise HTTPException(404, "Level not found")
    return level

@router.get("/")
async def list_levels(tournament_legal: bool | None = None):
    query = {}
    if tournament_legal is not None:
        query["tournament_legal"] = tournament_legal

    return await db.levels.find(query, {"_id": 0}).to_list(100)