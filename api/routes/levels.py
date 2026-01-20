from fastapi import APIRouter, HTTPException, Request
from ..db import db
from api.models.level import Level
from api.utils import limiter

router = APIRouter(prefix="/levels", tags=["levels"])

@router.get("/{code}")
@limiter.limit("30/minute")
async def get_level(request: Request, code: str):
    level = await db.levels.find_one({"code": code}, {"_id": 0})
    if not level:
        raise HTTPException(404, "Level not found")
    return level

@router.get("/")
@limiter.limit("30/minute")
async def list_levels(request: Request, tournament_legal: bool | None = None):
    query = {}
    if tournament_legal is not None:
        query["tournament_legal"] = tournament_legal

    return await db.levels.find(query, {"_id": 0}).to_list(100)