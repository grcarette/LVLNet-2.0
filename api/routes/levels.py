from fastapi import APIRouter, HTTPException, Request
from ..db import db
from api.models.level import Level
from api.utils import limiter

router = APIRouter(prefix="/levels", tags=["levels"])

@router.get("/{code}")
@limiter.limit("30/minute")
@router.get("/{code}")
@limiter.limit("30/minute")
async def get_level(request: Request, code: str):
    pipeline = [
        {"$match": {"code": code}},
        {
            "$lookup": {
                "from": "users",
                "localField": "creators", 
                "foreignField": "discord_id", 
                "as": "creator_objects"
            }
        },
        {
            "$project": {
                "_id": 0,
                "name": 1,
                "code": 1,
                "imgur_url": 1,
                "mode": 1,
                "tournament_legal": 1,
                "creators": {
                    "$map": {
                        "input": "$creator_objects",
                        "as": "user",
                        "in": {
                            "id": "$$user.discord_id",
                            "username": "$$user.username"
                        }
                    }
                }
            }
        }
    ]
    
    results = await db.levels.aggregate(pipeline).to_list(length=1)
    
    if not results:
        raise HTTPException(404, "Level not found")
        
    return results[0]

@router.get("/random/{amount}")
@limiter.limit("30/minute")
async def get_random_levels(request: Request, amount: int):
    amount = max(1, min(amount, 5))

    pipeline = [
        {"$match": {"tournament_legal": True}},
        
        {"$sample": {"size": amount}},
        {
            "$lookup": {
                "from": "users",
                "localField": "creators", 
                "foreignField": "discord_id", 
                "as": "creator_objects"
            }
        },
        {
            "$project": {
                "_id": 0,
                "name": 1,
                "code": 1,
                "imgur_url": 1,
                "mode": 1,
                "tournament_legal": 1,
                "creators": {
                    "$map": {
                        "input": "$creator_objects",
                        "as": "user",
                        "in": {
                            "id": "$$user.discord_id",
                            "username": "$$user.username"
                        }
                    }
                }
            }
        }
    ]
    
    return await db.levels.aggregate(pipeline).to_list(length=amount)

@router.get("/")
@limiter.limit("30/minute")
async def list_levels(
    request: Request, 
    tournament_legal: bool | None = None,
    mode: str = "party" 
):
    query = {"mode": mode.lower()}
    if tournament_legal is not None: 
        query["tournament_legal"] = True

    pipeline = [
        {"$match": query},
        {
            "$lookup": {
                "from": "users",
                "localField": "creators",    
                "foreignField": "discord_id", 
                "as": "creator_objects"
            }
        },
        {
            "$project": {
                "_id": 0,
                "name": 1,
                "code": 1,
                "imgur_url": 1,
                "mode": 1,
                "tournament_legal": 1,
                "creators": {
                    "$map": {
                        "input": "$creator_objects",
                        "as": "user",
                        "in": {
                            "id": "$$user.discord_id",
                            "username": "$$user.username"
                        }
                    }
                }
            }
        }
    ]

    return await db.levels.aggregate(pipeline).to_list(length=None)