import io
import secrets
import string

from fastapi import APIRouter, HTTPException, Request, Query, Form, File, UploadFile, Response, Depends
from typing import Optional, List
from datetime import datetime, timezone
from bson.binary import Binary
from PIL import Image, UnidentifiedImageError

from ..db import db
from api.utils import limiter, require_api_key

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
    """Generate a pack ID that isn't already taken in EITHER `packs` or
    `pack_drafts`. Collisions are astronomically unlikely (26**10), but there's no
    unique index on pack_id, so a duplicate would otherwise insert a second
    document silently. Checking both collections means a draft's ID survives
    publishing (the same ID moves from `pack_drafts` to `packs`)."""
    while True:
        pack_id = _generate_pack_id()
        in_packs = await db.packs.find_one({"pack_id": pack_id}, {"_id": 1})
        in_drafts = await db.pack_drafts.find_one({"pack_id": pack_id}, {"_id": 1})
        if in_packs is None and in_drafts is None:
            return pack_id


@router.post("/", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def create_pack(
    request: Request,
    name: str = Form(...),
    author: int = Form(...),
    levels: List[str] = Form(...),
    description: str = Form(""),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Create a pack from a name, author Discord ID, list of level codes, an
    optional description, and an optional thumbnail image.

    The pack's levels are FINAL: once created they can never be changed. Editing a
    pack afterward (PUT) only updates presentation fields (name, description,
    thumbnail). This is intentional — a pack is a fixed course, so leaderboard times
    submitted against it stay valid for the life of the pack.

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
        "name": name,
        "description": description,
        "thumbnail": thumbnail_doc,
        "levels": levels,
        "deleted": False,
        "created_at": now,
        "updated_at": now,
    }

    await db.packs.insert_one(pack_doc)
    # `version` is vestigial in the response for client compatibility; packs no
    # longer carry versions.
    return {"packId": pack_id, "version": 1, "levelCount": len(levels)}


@router.put("/{pack_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def update_pack(
    request: Request,
    pack_id: str,
    author: int = Form(..., description="discord_id of the user making the update"),
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Update a pack's presentation fields. The pack's LEVELS ARE FINAL and cannot
    be changed here — there is deliberately no `levels` parameter. Only `name`,
    `description`, and `thumbnail` may be edited; any field omitted is left as-is.

    The submitter (`author`) must be the original author of the pack. A supplied
    `thumbnail` runs through the same validate/downscale/PNG pipeline as creation.

    Sent as multipart/form-data: `author`, plus any of optional `name`,
    `description`, and `thumbnail`."""
    pack = await db.packs.find_one({"pack_id": pack_id, "deleted": {"$ne": True}})
    if not pack:
        raise HTTPException(404, "Pack not found")

    if pack["author"] != author:
        raise HTTPException(403, "You are not the author of this pack")

    now = datetime.now(timezone.utc)
    set_fields = {"updated_at": now}

    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(400, "Pack name must not be empty")
        set_fields["name"] = name

    if description is not None:
        set_fields["description"] = description

    if thumbnail is not None:
        set_fields["thumbnail"] = await _process_thumbnail(thumbnail)

    await db.packs.update_one({"pack_id": pack_id}, {"$set": set_fields})

    return {"packId": pack_id, "updated": True, "version": 1}


@router.delete("/{pack_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def delete_pack(
    request: Request,
    pack_id: str,
    author: int = Form(..., description="discord_id of the user requesting the delete"),
):
    """Soft-delete a pack by flagging `deleted: True`. The document is kept but
    is excluded from every other read endpoint.

    Requires a valid API key (header) AND that `author` matches the pack's
    original author, so a key holder can only delete their own packs. Sent as
    multipart/form-data with a single `author` field."""
    pack = await db.packs.find_one({"pack_id": pack_id, "deleted": {"$ne": True}})
    if not pack:
        raise HTTPException(404, "Pack not found")

    if pack["author"] != author:
        raise HTTPException(403, "You are not the author of this pack")

    await db.packs.update_one(
        {"pack_id": pack_id},
        {"$set": {"deleted": True}},
    )

    return {"packId": pack_id, "deleted": True}


@router.get("/")
@limiter.limit("60/minute")
async def list_packs(request: Request):
    pipeline = [
        # Exclude soft-deleted packs
        {"$match": {"deleted": {"$ne": True}}},
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
                # Vestigial; kept for client compatibility.
                "latestVersion": {"$literal": 1},
                "name": "$name",
                "authorId": "$author",
                "author": {"$arrayElemAt": ["$_author.username", 0]},
                "thumbnailUrl": {
                    "$cond": [
                        {"$ifNull": ["$thumbnail", False]},
                        {"$concat": ["/packs/", "$pack_id", "/thumbnail"]},
                        None
                    ]
                },
                "levelCount": {"$size": {"$ifNull": ["$levels", []]}}
            }
        }
    ]
    packs = await db.packs.aggregate(pipeline).to_list(length=None)
    return {"packs": packs}


@router.get("/{pack_id}")
@limiter.limit("120/minute")
async def get_pack(request: Request, pack_id: str):
    pack = await db.packs.find_one({"pack_id": pack_id, "deleted": {"$ne": True}})
    if not pack:
        raise HTTPException(404, "Pack not found")

    author = await db.users.find_one({"discord_id": pack["author"]})
    author_name = author["username"] if author else "Unknown User"

    thumbnail_url = None
    if pack.get("thumbnail"):
        thumbnail_url = f"/packs/{pack['pack_id']}/thumbnail"

    return {
        "packId": pack["pack_id"],
        # Vestigial; kept for client compatibility.
        "version": 1,
        "name": pack["name"],
        "authorId": pack["author"],
        "author": author_name,
        "description": pack.get("description", ""),
        "thumbnailUrl": thumbnail_url,
        "levels": pack.get("levels", []),
        "createdAt": pack["created_at"],
        "updatedAt": pack["updated_at"],
    }


@router.get("/{pack_id}/versions")
@limiter.limit("120/minute")
async def get_pack_versions(request: Request, pack_id: str):
    """DEPRECATED compatibility shim. Packs no longer have versions; this always
    reports a single version so older clients don't break. Safe to delete once the
    client stops calling it."""
    pack = await db.packs.find_one(
        {"pack_id": pack_id, "deleted": {"$ne": True}},
        {"_id": 0, "pack_id": 1, "levels": 1, "created_at": 1},
    )
    if not pack:
        raise HTTPException(404, "Pack not found")

    return {
        "packId": pack["pack_id"],
        "versions": [
            {
                "version": 1,
                "createdAt": pack["created_at"],
                "levelCount": len(pack.get("levels", [])),
            }
        ],
    }


@router.get("/{pack_id}/thumbnail")
@limiter.limit("120/minute")
async def get_pack_thumbnail(request: Request, pack_id: str):
    pack = await db.packs.find_one(
        {"pack_id": pack_id, "deleted": {"$ne": True}},
        {"_id": 0, "thumbnail": 1},
    )
    if not pack:
        raise HTTPException(404, "Pack not found")

    thumb = pack.get("thumbnail")
    if not thumb:
        raise HTTPException(404, "Thumbnail not found")

    return Response(
        content=bytes(thumb["data"]),
        media_type=thumb.get("content_type", "image/png"),
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/by-author/{discord_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute")
async def list_packs_by_author(request: Request, discord_id: int):
    """All of a user's packs — published AND work-in-progress drafts — in one
    list, each tagged with a `status` of "published" or "draft".

    Because this returns private drafts, it requires the API key (unlike the
    public `GET /packs/` listing). Results are sorted most-recently-updated first.
    Draft thumbnails are served from `/packs/drafts/{id}/thumbnail`; published ones
    from `/packs/{id}/thumbnail`."""
    published = await db.packs.find(
        {"author": discord_id, "deleted": {"$ne": True}}
    ).to_list(length=None)
    drafts = await db.pack_drafts.find({"author": discord_id}).to_list(length=None)

    user = await db.users.find_one({"discord_id": discord_id})
    author_name = user["username"] if user else "Unknown User"

    items = []
    for p in published:
        items.append({
            "packId": p["pack_id"],
            "status": "published",
            "name": p.get("name", ""),
            "levelCount": len(p.get("levels", [])),
            "thumbnailUrl": f"/packs/{p['pack_id']}/thumbnail" if p.get("thumbnail") else None,
            "createdAt": p.get("created_at"),
            "updatedAt": p.get("updated_at"),
        })
    for d in drafts:
        items.append({
            "packId": d["pack_id"],
            "status": "draft",
            "name": d.get("name", ""),
            "levelCount": len(d.get("levels", [])),
            "thumbnailUrl": f"/packs/drafts/{d['pack_id']}/thumbnail" if d.get("thumbnail") else None,
            "createdAt": d.get("created_at"),
            "updatedAt": d.get("updated_at"),
        })

    items.sort(key=lambda x: x.get("updatedAt") or x.get("createdAt"), reverse=True)
    return {"authorId": discord_id, "author": author_name, "packs": items}