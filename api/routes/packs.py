from fastapi import APIRouter, HTTPException, Request, Query, Body
from typing import Optional, List
from datetime import datetime, timezone
from uuid import uuid4

from ..db import db
from api.utils import limiter

router = APIRouter(prefix="/packs", tags=["packs"])


@router.post("/")
@limiter.limit("30/minute")
async def create_pack(request: Request, levels: List[str] = Body(..., embed=True)):
    """Quick test endpoint: create a pack from just a list of level codes.
    Everything else is filled with placeholder defaults."""
    now = datetime.now(timezone.utc)
    pack_id = str(uuid4())

    pack_doc = {
        "pack_id": pack_id,
        "author": 0,
        "latest_version": 1,
        "created_at": now,
        "updated_at": now,
        "versions": [
            {
                "version": 1,
                "name": f"Untitled Pack ({pack_id[:8]})",
                "description": "",
                "thumbnail_url": None,
                "levels": levels,
                "created_at": now,
            }
        ],
    }

    await db.packs.insert_one(pack_doc)
    return {"packId": pack_id, "version": 1, "levelCount": len(levels)}


@router.get("/")
@limiter.limit("60/minute")
async def list_packs(request: Request):
    pipeline = [
        # Pull out the version object that matches latest_version
        {
            "$addFields": {
                "_latest": {
                    "$arrayElemAt": [
                        {
                            "$filter": {
                                "input": "$versions",
                                "as": "v",
                                "cond": {"$eq": ["$$v.version", "$latest_version"]}
                            }
                        },
                        0
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "users",
                "localField": "author",
                "foreignField": "discord_id",
                "as": "_author"
            }
        },
        {
            "$project": {
                "_id": 0,
                "packId": "$pack_id",
                "latestVersion": "$latest_version",
                "name": "$_latest.name",
                "author": {"$arrayElemAt": ["$_author.username", 0]},
                "thumbnailUrl": "$_latest.thumbnail_url",
                "levelCount": {"$size": {"$ifNull": ["$_latest.levels", []]}}
            }
        }
    ]
    packs = await db.packs.aggregate(pipeline).to_list(length=None)
    return {"packs": packs}


@router.get("/{pack_id}")
@limiter.limit("120/minute")
async def get_pack(
    request: Request,
    pack_id: str,
    version: Optional[int] = Query(None, description="Specific version (omit for latest)")
):
    pack = await db.packs.find_one({"pack_id": pack_id})
    if not pack:
        raise HTTPException(404, "Pack not found")

    target = version if version is not None else pack["latest_version"]
    version_data = next(
        (v for v in pack["versions"] if v["version"] == target),
        None
    )
    if version_data is None:
        raise HTTPException(404, f"Version {target} not found for pack {pack_id}")

    author = await db.users.find_one({"discord_id": pack["author"]})
    author_name = author["username"] if author else "Unknown User"

    return {
        "packId": pack["pack_id"],
        "version": version_data["version"],
        "name": version_data["name"],
        "author": author_name,
        "description": version_data.get("description", ""),
        "thumbnailUrl": version_data.get("thumbnail_url"),
        "levels": version_data.get("levels", []),
        "createdAt": version_data["created_at"],
        "updatedAt": pack["updated_at"],
    }


@router.get("/{pack_id}/versions")
@limiter.limit("120/minute")
async def get_pack_versions(request: Request, pack_id: str):
    pack = await db.packs.find_one(
        {"pack_id": pack_id},
        {"_id": 0, "pack_id": 1, "versions": 1}
    )
    if not pack:
        raise HTTPException(404, "Pack not found")

    versions = [
        {
            "version": v["version"],
            "createdAt": v["created_at"],
            "levelCount": len(v.get("levels", [])),
        }
        for v in sorted(pack["versions"], key=lambda v: v["version"])
    ]

    return {"packId": pack["pack_id"], "versions": versions}