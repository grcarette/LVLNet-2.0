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
async def list_levels(
    request: Request, 
    tournament_legal: bool | None = None,
    mode: str = "party" 
):
    query = {}
    query["mode"] = mode.lower()

    if tournament_legal is None or tournament_legal is True:
        query["tournament_legal"] = True
    else:
        pass 

    return await db.levels.find(query, {"_id": 0}).to_list(length=None)