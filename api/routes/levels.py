from fastapi import APIRouter, HTTPException, Request, Body, Depends
from ..db import db
from ..imgur import get_imgur_data
from api.models.level import LevelCreateRequest
from api.utils import limiter, require_api_key
from typing import List

router = APIRouter(prefix="/levels", tags=["levels"])

@router.post("/batch")
@limiter.limit("60/minute")
async def get_levels_from_list(request: Request, codes: List[str] = Body(...)):
    pipeline = [
        {"$match": {"code": {"$in": codes}, "hidden": {"$ne": True}}},
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
    results = await db.levels.aggregate(pipeline).to_list(length=len(codes))
    
    return results

@router.get("/{code}")
@limiter.limit("120/minute")
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
@limiter.limit("120/minute")
async def get_random_levels(request: Request, amount: int):
    amount = max(1, min(amount, 5))

    pipeline = [
        {"$match": {"tournament_legal": True, "hidden": {"$ne": True}}},
        
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
    query = {"mode": mode.lower(), "hidden": {"$ne": True}}
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

VALID_MODES = ("party", "challenge")


def _is_valid_level_code(code: str) -> bool:
    return len(code) == 9 and code[4] == "-"


async def _upload_one(body: LevelCreateRequest):
    """Resolve, validate, and write a single level.
    Returns (result, None) on success or (None, (status, reason)) on failure."""
    mode = body.mode.lower()
    if mode not in VALID_MODES:
        return None, (400, f"mode must be one of {VALID_MODES}")

    imgur_data = await get_imgur_data(body.imgur_url)
    if not imgur_data:
        return None, (400, "Could not resolve the Imgur link")

    code = imgur_data["code"]
    if not _is_valid_level_code(code):
        return None, (400, "Imgur description is not a valid level code")

    creator_ids = body.creators
    existing = await db.levels.find_one({"code": code})

    if existing:
        if (
            existing.get("hidden")
            and not body.hidden
            and any(cid in existing["creators"] for cid in creator_ids)
        ):
            update = {
                "imgur_url": body.imgur_url,
                "name": imgur_data["title"],
                "creators": creator_ids,
                "mode": mode,
                "hidden": False,
            }
            await db.levels.update_one({"code": code}, {"$set": update})
            return {
                "code": code,
                "created": False,
                "unhidden": True,
                "tournament_legal": existing.get("tournament_legal", False),
                **update,
            }, None

        return None, (409, "A level with that code already exists")

    level_doc = {
        "imgur_url": body.imgur_url,
        "name": imgur_data["title"],
        "code": code,
        "mode": mode,
        "creators": creator_ids,
        "tournament_legal": False,
        "hidden": body.hidden,
    }
    await db.levels.insert_one(level_doc)
    return {
        "code": code,
        "name": level_doc["name"],
        "imgur_url": level_doc["imgur_url"],
        "mode": mode,
        "creators": creator_ids,
        "hidden": body.hidden,
        "tournament_legal": False,
        "created": True,
        "unhidden": False,
    }, None


@router.post("/", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def create_level(request: Request, body: LevelCreateRequest):
    result, error = await _upload_one(body)
    if error:
        raise HTTPException(error[0], error[1])
    return result


@router.post("/bulk", dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
async def bulk_create_levels(
    request: Request, body: List[LevelCreateRequest] = Body(...)
):
    if not body:
        raise HTTPException(400, "Request body must contain at least one level")

    uploaded, failed = [], []
    for item in body:
        result, error = await _upload_one(item)
        if error:
            failed.append({"imgur_url": item.imgur_url, "reason": error[1]})
        else:
            uploaded.append(result)

    return {"uploaded": uploaded, "failed": failed}