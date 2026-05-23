import io
import secrets
import string

from fastapi import APIRouter, HTTPException, Request, Query, Form, File, UploadFile, Response
from typing import Optional, List
from datetime import datetime, timezone
from bson.binary import Binary
from PIL import Image, UnidentifiedImageError

from ..db import db
from api.utils import limiter

router = APIRouter(prefix="/packs", tags=["packs"])

# Pack IDs look like ABCDE-FGHIJ (two 5-char blocks of capital letters).
PACK_ID_ALPHABET = string.ascii_uppercase
PACK_ID_BLOCK_LEN = 5

# A pack must contain at least this many level codes.
MIN_PACK_LEVELS = 3

# Thumbnails are normalized server-side: anything a client sends is decoded,
# downscaled to fit within these bounds (aspect ratio preserved, never upscaled),
# and re-encoded to PNG. Nothing the caller claims about the file is trusted.
MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024  # 5 MB
THUMBNAIL_BOUNDS = (1024, 768)


async def _process_thumbnail(upload: UploadFile) -> dict:
    """Read, validate, downscale, and re-encode an uploaded thumbnail to PNG.

    The stored content_type is derived from the bytes we produce, not from anything
    the caller claimed, so it always matches. Re-encoding strips embedded scripts and
    metadata, and the downscaled result is a few KB. Raises HTTPException(400) for
    anything that isn't a decodable raster image or that exceeds the size cap. Animated
    inputs (e.g. GIF) are flattened to their first frame."""
    raw = await upload.read(MAX_THUMBNAIL_BYTES + 1)
    if len(raw) > MAX_THUMBNAIL_BYTES:
        raise HTTPException(400, f"Thumbnail exceeds the {MAX_THUMBNAIL_BYTES // (1024 * 1024)}MB limit")
    if not raw:
        raise HTTPException(400, "Thumbnail file is empty")

    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()  # force-decode the first frame
            has_alpha = (
                img.mode in ("RGBA", "LA")
                or (img.mode == "P" and "transparency" in img.info)
            )
            img = img.convert("RGBA" if has_alpha else "RGB")
            img.thumbnail(THUMBNAIL_BOUNDS)  # preserves aspect ratio, only downscales

            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(400, "Thumbnail must be a valid image file")

    return {
        "data": Binary(out.getvalue()),
        "content_type": "image/png",
    }


def _generate_pack_id() -> str:
    block = lambda: "".join(secrets.choice(PACK_ID_ALPHABET) for _ in range(PACK_ID_BLOCK_LEN))
    return f"{block()}-{block()}"


async def _generate_unique_pack_id() -> str:
    """Generate a pack ID that isn't already taken. Collisions are astronomically
    unlikely (26**10), but there's no unique index on pack_id, so a duplicate would
    otherwise insert a second document silently."""
    while True:
        pack_id = _generate_pack_id()
        existing = await db.packs.find_one({"pack_id": pack_id}, {"_id": 1})
        if existing is None:
            return pack_id


@router.post("/")
@limiter.limit("30/minute")
async def create_pack(
    request: Request,
    name: str = Form(...),
    author: int = Form(...),
    levels: List[str] = Form(...),
    description: str = Form(""),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Create a pack (version 1) from a name, author Discord ID, list of level
    codes, an optional description, and an optional thumbnail image.

    Sent as multipart/form-data: `name`, `author`, repeated `levels` fields, plus
    optional `description` and `thumbnail`.

    The author is stored as a Discord ID; the GET endpoints resolve it to a username
    via the `users` collection. An author that isn't registered yet is accepted and
    will simply show as "Unknown User" until they're registered elsewhere.

    Any common image format is accepted for `thumbnail`; it's validated, downscaled,
    and converted to PNG server-side, so a non-image or oversized file returns 400.

    `name` and `author` are required, and at least three (non-blank) level codes must
    be supplied; otherwise the request is rejected with a 400."""
    name = name.strip()
    if not name:
        raise HTTPException(400, "Pack name must not be empty")

    levels = [code.strip() for code in levels if code.strip()]
    if len(levels) < MIN_PACK_LEVELS:
        raise HTTPException(400, f"A pack must contain at least {MIN_PACK_LEVELS} levels")

    now = datetime.now(timezone.utc)
    pack_id = await _generate_unique_pack_id()

    thumbnail_doc = None
    if thumbnail is not None:
        thumbnail_doc = await _process_thumbnail(thumbnail)

    pack_doc = {
        "pack_id": pack_id,
        "author": author,
        "latest_version": 1,
        "created_at": now,
        "updated_at": now,
        "versions": [
            {
                "version": 1,
                "name": name,
                "description": description,
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
        media_type=thumb.get("content_type", "image/png"),
        headers={"X-Content-Type-Options": "nosniff"},
    )