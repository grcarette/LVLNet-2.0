import io
import logging
import secrets
import string

from fastapi import APIRouter, HTTPException, Request, Query, Form, File, UploadFile, Response, Depends
from typing import Optional, List
from datetime import datetime, timezone
from bson.binary import Binary
from PIL import Image, UnidentifiedImageError

from ..db import db, client
from ..scoring import wilson_lower_bound
from ..auth import require_session, enforce_gsid
from api.models.rating import VoteRequest
from api.utils import limiter, require_api_key

logger = logging.getLogger("lvlnet.packs")

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

# Listing / paging (spec §4.2).
VALID_FILTERS = ("featured", "toprated", "newest", "mylevels")
DEFAULT_FILTER = "newest"
DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 100

# Allowed vote values (spec §2 / §4.1): up, down, retract.
VALID_VOTE_VALUES = (1, -1, 0)


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
    """Generate a pack ID that isn't taken in EITHER `packs` or `pack_drafts`.
    The id must be unique across both collections because it survives the
    draft -> published -> unpublished -> re-published lifecycle unchanged."""
    while True:
        pack_id = _generate_pack_id()
        in_packs = await db.packs.find_one({"pack_id": pack_id}, {"_id": 1})
        in_drafts = await db.pack_drafts.find_one({"pack_id": pack_id}, {"_id": 1})
        if in_packs is None and in_drafts is None:
            return pack_id


# --------------------------------------------------------------------------- #
# Accounts / ratings helpers (spec §1.3, §2)
# --------------------------------------------------------------------------- #

async def ensure_account(gsid: str) -> None:
    """Auto-create the account for `gsid` on first contact (spec §1.3).

    Idempotent upsert: `$setOnInsert` only writes the seed fields the first time,
    so an existing account (and any later-paired discord_id) is never touched.
    """
    await db.accounts.update_one(
        {"gsid": gsid},
        {
            "$setOnInsert": {
                "gsid": gsid,
                "discord_id": None,
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


def _vote_delta(old_value: int, new_value: int) -> tuple[int, int]:
    """Counter deltas (d_ups, d_downs) for a vote transition (spec §2)."""
    d_ups = d_downs = 0
    if old_value == 1:
        d_ups -= 1
    elif old_value == -1:
        d_downs -= 1
    if new_value == 1:
        d_ups += 1
    elif new_value == -1:
        d_downs += 1
    return d_ups, d_downs


async def _apply_vote(pack_id: str, gsid: str, new_value: int) -> tuple[int, int]:
    """Apply a vote and keep the pack's denormalized counters in sync, atomically."""
    now = datetime.now(timezone.utc)
    async with await client.start_session() as session:
        async with session.start_transaction():
            pack = await db.packs.find_one(
                {"pack_id": pack_id},
                {"_id": 0, "ups": 1, "downs": 1},
                session=session,
            )
            cur_ups = (pack or {}).get("ups", 0) or 0
            cur_downs = (pack or {}).get("downs", 0) or 0

            existing = await db.ratings.find_one(
                {"pack_id": pack_id, "gsid": gsid},
                {"_id": 0, "value": 1},
                session=session,
            )
            old_value = existing["value"] if existing else 0

            # Source of truth first: upsert the rating, or delete it on retract.
            if new_value == 0:
                if existing is not None:
                    await db.ratings.delete_one(
                        {"pack_id": pack_id, "gsid": gsid}, session=session
                    )
            else:
                await db.ratings.update_one(
                    {"pack_id": pack_id, "gsid": gsid},
                    {"$set": {"value": new_value, "updated_at": now}},
                    upsert=True,
                    session=session,
                )

            d_ups, d_downs = _vote_delta(old_value, new_value)
            new_ups = cur_ups + d_ups
            new_downs = cur_downs + d_downs
            new_wilson = wilson_lower_bound(new_ups, new_downs)

            await db.packs.update_one(
                {"pack_id": pack_id},
                {
                    "$inc": {"ups": d_ups, "downs": d_downs},
                    "$set": {"wilson": new_wilson},
                },
                session=session,
            )

    return new_ups, new_downs


@router.post("/", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def create_pack(
    request: Request,
    name: str = Form(...),
    author: str = Form(...),
    levels: List[str] = Form(...),
    description: str = Form(""),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Admin/bot path (API key): create a pack directly. `author` is the
    author's gsid string (legacy rows may still hold Discord-id ints; reads
    coerce with $toString)."""
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
        "author": author.strip(),
        "name": name,
        "description": description,
        "thumbnail": thumbnail_doc,
        "levels": levels,
        "deleted": False,
        # Denormalized rating aggregates kept in sync on every vote (spec §1.2).
        "ups": 0,
        "downs": 0,
        "wilson": 0.0,
        # Reserved for a future featured-setter; no endpoint sets it yet (spec §8).
        "featured": False,
        "created_at": now,
        "updated_at": now,
    }

    await db.packs.insert_one(pack_doc)
    return {"packId": pack_id, "levelCount": len(levels)}


@router.put("/{pack_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def update_pack(
    request: Request,
    pack_id: str,
    author: str = Form(..., description="gsid of the user making the update"),
    levels: List[str] = Form(...),
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Admin/bot path (API key): update a pack's contents in place."""
    pack = await db.packs.find_one({"pack_id": pack_id, "deleted": {"$ne": True}})
    if not pack:
        raise HTTPException(404, "Pack not found")

    if str(pack["author"]) != author.strip():
        raise HTTPException(403, "You are not the author of this pack")

    levels = [code.strip() for code in levels if code.strip()]
    if len(levels) < MIN_PACK_LEVELS:
        raise HTTPException(400, f"A pack must contain at least {MIN_PACK_LEVELS} levels")

    now = datetime.now(timezone.utc)

    set_fields = {"levels": levels, "updated_at": now}

    if thumbnail is not None:
        set_fields["thumbnail"] = await _process_thumbnail(thumbnail)

    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(400, "Pack name must not be empty")
        set_fields["name"] = name

    if description is not None:
        set_fields["description"] = description

    await db.packs.update_one({"pack_id": pack_id}, {"$set": set_fields})

    return {"packId": pack_id, "levelCount": len(levels)}


@router.delete("/{pack_id}")
@limiter.limit("30/minute")
async def delete_pack(
    request: Request,
    pack_id: str,
    token_gsid: str = Depends(require_session),
    gsid: str = Form(""),
):
    """Soft-delete a published pack by flagging `deleted: True`.

    Requires a bearer session token; the token's gsid must be the pack's author.
    The optional `gsid` form field, if sent, must agree with the token."""
    caller = enforce_gsid(token_gsid, gsid)

    pack = await db.packs.find_one({"pack_id": pack_id, "deleted": {"$ne": True}})
    if not pack:
        raise HTTPException(404, "Pack not found")

    if str(pack["author"]) != caller:
        raise HTTPException(403, "You are not the author of this pack")

    await db.packs.update_one(
        {"pack_id": pack_id},
        {"$set": {"deleted": True}},
    )

    return {"packId": pack_id, "deleted": True}


@router.post("/{pack_id}/unpublish")
@limiter.limit("30/minute")
async def unpublish_pack(
    request: Request,
    pack_id: str,
    token_gsid: str = Depends(require_session),
    gsid: str = Form(""),
):
    """Revert a published pack to a draft (Feature 2).

    Auth: bearer token required; the token's gsid must equal the pack's author
    (`authorId`), otherwise 403. A `gsid` form field that disagrees with the
    token is rejected with 401.

    Atomically (single transaction) moves the document from `packs` into
    `pack_drafts`, preserving the packId, name, author, description, ordered
    level list, thumbnail, and original created_at. Rating counters and the
    published level list are snapshotted onto the draft so re-publish can
    restore votes and decide whether the leaderboard is still valid. Times and
    ratings rows are RETAINED in their collections — they are hidden publicly
    simply because the pack is no longer in `packs` (lists, detail, and the
    times endpoint all key off that collection).

    Response (checked literally by the client): {"status": "draft", "packId": ...}."""
    caller = enforce_gsid(token_gsid, gsid)

    pack = await db.packs.find_one({"pack_id": pack_id, "deleted": {"$ne": True}})
    if not pack:
        # Unknown, deleted, or already a draft -> 404 (spec allows 404 here).
        raise HTTPException(404, "Pack not found or not currently published")

    if str(pack["author"]) != caller:
        raise HTTPException(403, "You are not the author of this pack")

    now = datetime.now(timezone.utc)
    draft_doc = {
        "pack_id": pack["pack_id"],          # MUST NOT change
        "author": pack["author"],
        "name": pack.get("name", ""),
        "description": pack.get("description", ""),
        "thumbnail": pack.get("thumbnail"),
        "levels": pack.get("levels", []),    # full ordered list
        "created_at": pack["created_at"],    # preserved
        "updated_at": now,
        # Bookkeeping for re-publish (see drafts.publish_draft):
        "published_created_at": pack["created_at"],
        "published_levels": list(pack.get("levels", [])),
        "published_ratings": {
            "ups": pack.get("ups", 0) or 0,
            "downs": pack.get("downs", 0) or 0,
            "wilson": pack.get("wilson", 0.0) or 0.0,
            "featured": bool(pack.get("featured", False)),
        },
    }

    async with await client.start_session() as session:
        async with session.start_transaction():
            await db.pack_drafts.insert_one(draft_doc, session=session)
            await db.packs.delete_one({"pack_id": pack_id}, session=session)

    logger.info("pack %s unpublished by gsid %s", pack_id, caller)
    return {"status": "draft", "packId": pack_id}


@router.post("/{pack_id}/vote")
@limiter.limit("60/minute")
async def vote_pack(request: Request, pack_id: str, body: VoteRequest):
    """Set, change, or retract the caller's vote on a pack (spec §4.1)."""
    gsid = (body.gsid or "").strip()
    if not gsid:
        raise HTTPException(400, "gsid is required")
    if body.value not in VALID_VOTE_VALUES:
        raise HTTPException(400, "value must be 1, -1, or 0")

    pack = await db.packs.find_one(
        {"pack_id": pack_id, "deleted": {"$ne": True}}, {"_id": 1}
    )
    if not pack:
        raise HTTPException(404, "Pack not found")

    await ensure_account(gsid)
    new_ups, new_downs = await _apply_vote(pack_id, gsid, body.value)

    return {"ups": new_ups, "downs": new_downs, "myVote": body.value}


@router.get("/")
@limiter.limit("60/minute")
async def list_packs(
    request: Request,
    filter: str = Query(DEFAULT_FILTER, description="featured | toprated | newest | mylevels"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    gsid: Optional[str] = Query(None, description="caller's gsid; required for mylevels, enriches myVote"),
):
    """Paged pack summaries enriched with rating data (spec §4.2). `authorId`
    is serialized as a string (the author's gsid; legacy rows holding a Discord
    id are coerced with $toString so the client always sees a string)."""
    if filter not in VALID_FILTERS:
        raise HTTPException(400, f"filter must be one of {VALID_FILTERS}")

    gsid = gsid.strip() if gsid else None
    if gsid:
        # First gsid-bearing list call also auto-registers the account (spec §1.3).
        await ensure_account(gsid)

    base_match = {"deleted": {"$ne": True}}

    if filter == "mylevels":
        if not gsid:
            raise HTTPException(400, "gsid is required for the mylevels filter")
        # Authors are gsid strings; legacy packs may still carry a paired
        # Discord id, so match either.
        author_keys: list = [gsid]
        account = await db.accounts.find_one({"gsid": gsid}, {"_id": 0, "discord_id": 1})
        if account and account.get("discord_id") is not None:
            author_keys.append(account["discord_id"])
        base_match["author"] = {"$in": author_keys}

    # Normalize denormalized fields so legacy packs (created before ratings) still
    # sort correctly with sensible defaults.
    normalize = {
        "$addFields": {
            "_ups": {"$ifNull": ["$ups", 0]},
            "_downs": {"$ifNull": ["$downs", 0]},
            "_wilson": {"$ifNull": ["$wilson", 0]},
            "_featured": {"$ifNull": ["$featured", False]},
        }
    }

    if filter == "toprated":
        normalize["$addFields"]["_hasVotes"] = {"$gt": [{"$add": ["$_ups", "$_downs"]}, 0]}
        sort_spec = {"_hasVotes": -1, "_wilson": -1, "created_at": -1}
    elif filter == "featured":
        sort_spec = {"_featured": -1, "created_at": -1}
    else:  # newest, mylevels
        sort_spec = {"created_at": -1}

    page_pipeline = [
        {"$skip": (page - 1) * pageSize},
        {"$limit": pageSize},
        {
            "$lookup": {
                "from": "users",
                "localField": "author",
                "foreignField": "discord_id",
                "as": "_author",
            }
        },
        {
            "$project": {
                "_id": 0,
                "packId": "$pack_id",
                "name": "$name",
                # Always a string on the wire (gsid, or stringified legacy id).
                "authorId": {"$toString": "$author"},
                "author": {"$arrayElemAt": ["$_author.username", 0]},
                "thumbnailUrl": {
                    "$cond": [
                        {"$ifNull": ["$thumbnail", False]},
                        {"$concat": ["/packs/", "$pack_id", "/thumbnail"]},
                        None,
                    ]
                },
                "levelCount": {"$size": {"$ifNull": ["$levels", []]}},
                "ups": "$_ups",
                "downs": "$_downs",
                "featured": "$_featured",
                "createdAt": "$created_at",
            }
        },
    ]

    pipeline = [
        {"$match": base_match},
        normalize,
        {"$sort": sort_spec},
        {"$facet": {"data": page_pipeline, "meta": [{"$count": "total"}]}},
    ]

    result = await db.packs.aggregate(pipeline).to_list(length=1)
    facet = result[0] if result else {"data": [], "meta": []}
    data = facet.get("data", [])
    total = facet["meta"][0]["total"] if facet.get("meta") else 0

    # myVote: a single keyed query over (pack_id, gsid) for just this page's packs.
    if gsid and data:
        pack_ids = [p["packId"] for p in data]
        cursor = db.ratings.find(
            {"gsid": gsid, "pack_id": {"$in": pack_ids}},
            {"_id": 0, "pack_id": 1, "value": 1},
        )
        votes = {r["pack_id"]: r["value"] async for r in cursor}
        for p in data:
            p["myVote"] = votes.get(p["packId"], 0)
    else:
        for p in data:
            p["myVote"] = 0

    return {
        "packs": data,
        "page": page,
        "pageSize": pageSize,
        "total": total,
        "hasMore": page * pageSize < total,
    }


@router.get("/{pack_id}")
@limiter.limit("120/minute")
async def get_pack(
    request: Request,
    pack_id: str,
    gsid: Optional[str] = Query(None, description="caller's gsid; includes ups/downs/myVote when supplied"),
):
    """Single pack's current state. `authorId` is the author's gsid serialized
    as a string — the client's owner check (Unpublish/Delete buttons) reads it
    from this response. Unpublished packs are absent from `packs`, so they
    return 404 here, consistent with how drafts are hidden."""
    pack = await db.packs.find_one({"pack_id": pack_id, "deleted": {"$ne": True}})
    if not pack:
        raise HTTPException(404, "Pack not found")

    author = await db.users.find_one({"discord_id": pack["author"]})
    author_name = author["username"] if author else "Unknown User"

    thumbnail_url = None
    if pack.get("thumbnail"):
        thumbnail_url = f"/packs/{pack['pack_id']}/thumbnail"

    response = {
        "packId": pack["pack_id"],
        "name": pack["name"],
        "authorId": str(pack["author"]),
        "author": author_name,
        "description": pack.get("description", ""),
        "thumbnailUrl": thumbnail_url,
        "levels": pack.get("levels", []),
        "createdAt": pack["created_at"],
        "updatedAt": pack["updated_at"],
        "featured": bool(pack.get("featured", False)),
    }

    gsid = gsid.strip() if gsid else None
    if gsid:
        await ensure_account(gsid)
        rating = await db.ratings.find_one(
            {"pack_id": pack_id, "gsid": gsid}, {"_id": 0, "value": 1}
        )
        response["ups"] = pack.get("ups", 0) or 0
        response["downs"] = pack.get("downs", 0) or 0
        response["myVote"] = rating["value"] if rating else 0

    return response


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