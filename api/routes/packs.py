from fastapi import APIRouter, HTTPException, Request, Query, Form, File, UploadFile, Response
from typing import Optional, List
from datetime import datetime, timezone
from uuid import uuid4
from bson.binary import Binary

from ..db import db
from api.utils import limiter

router = APIRouter(prefix="/packs", tags=["packs"])


@router.post("/")
@limiter.limit("30/minute")
async def create_pack(
    request: Request,
    levels: List[str] = Form(...),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Quick test endpoint: create a pack from a list of level codes and an
    optional thumbnail image. Everything else is filled with placeholder defaults.

    Sent as multipart/form-data: repeated `levels` fields plus an optional
    `thumbnail` file."""
    now = datetime.now(timezone.utc)
    pack_id = str(uuid4())

    thumbnail_doc = None
    if thumbnail is not None:
        thumbnail_doc = {
            "data": Binary(await thumbnail.read()),
            "content_type": thumbnail.content_type or "application/octet-stream",
        }

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
                "thumbnail": thumbnail_doc,
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
                "thumbnailUrl": {
                    "$cond": [
                        {"$ifNull": ["$_latest.thumbnail", False]},
                        {"$concat": ["/packs/", "$pack_id", "/thumbnail"]},
                        None
                    ]
                },
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

    thumbnail_url = None
    if version_data.get("thumbnail"):
        thumbnail_url = f"/packs/{pack['pack_id']}/thumbnail?version={version_data['version']}"

    return {
        "packId": pack["pack_id"],
        "version": version_data["version"],
        "name": version_data["name"],
        "author": author_name,
        "description": version_data.get("description", ""),
        "thumbnailUrl": thumbnail_url,
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


@router.get("/{pack_id}/thumbnail")
@limiter.limit("120/minute")
async def get_pack_thumbnail(
    request: Request,
    pack_id: str,
    version: Optional[int] = Query(None, description="Specific version (omit for latest)")
):
    pack = await db.packs.find_one(
        {"pack_id": pack_id},
        {"_id": 0, "latest_version": 1, "versions": 1},
    )
    if not pack:
        raise HTTPException(404, "Pack not found")

    target = version if version is not None else pack["latest_version"]
    version_data = next(
        (v for v in pack["versions"] if v["version"] == target),
        None
    )
    if version_data is None:
        raise HTTPException(404, f"Version {target} not found for pack {pack_id}")

    thumb = version_data.get("thumbnail")
    if not thumb:
        raise HTTPException(404, "Thumbnail not found")

    return Response(
        content=bytes(thumb["data"]),
        media_type=thumb.get("content_type", "application/octet-stream"),
    )