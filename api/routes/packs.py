import io
import secrets
import string

from fastapi import APIRouter, HTTPException, Request, Query, Form, File, UploadFile, Response, Depends
from typing import Optional, List
from datetime import datetime, timezone
from bson.binary import Binary
from PIL import Image, UnidentifiedImageError

from ..db import db, client
from ..scoring import wilson_lower_bound
from api.models.rating import VoteRequest
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
    """Generate a pack ID that isn't already taken. Collisions are astronomically
    unlikely (26**10), but there's no unique index on pack_id, so a duplicate would
    otherwise insert a second document silently."""
    while True:
        pack_id = _generate_pack_id()
        existing = await db.packs.find_one({"pack_id": pack_id}, {"_id": 1})
        if existing is None:
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
    """Counter deltas (d_ups, d_downs) for a vote transition (spec §2).

    Values are 0 (none), 1 (up), -1 (down). Covers every case in the spec's
    table, including no-ops (old == new) and switches, by removing the old
    contribution and adding the new one."""
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
    """Apply a vote and keep the pack's denormalized counters in sync, atomically.

    The ratings-store change and the pack counter/wilson update run in a single
    multi-document transaction (Atlas replica set), so concurrent voters never lose
    a write and counters can't drift (spec §2, §6). Returns the pack's (ups, downs)
    after the write. Counter updates are incremental ($inc by the delta), never a
    recount."""
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

            # $inc by the delta (0 deltas still backfill missing fields on legacy
            # pack docs) and recompute wilson from the new counts, same transaction.
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
    author: int = Form(...),
    levels: List[str] = Form(...),
    description: str = Form(""),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Create a pack from a name, author Discord ID, list of level codes, an
    optional description, and an optional thumbnail image.

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
    author: int = Form(..., description="discord_id of the user making the update"),
    levels: List[str] = Form(...),
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Update a pack's contents in place.

    The submitter (`author`) must be the original author of the pack. `levels` is a
    full replacement, not a merge, and is held to the same `MIN_PACK_LEVELS` floor as
    creation. `name`, `description`, and `thumbnail` are optional: if omitted they are
    left unchanged. A supplied `thumbnail` runs through the same validate/downscale/PNG
    pipeline as creation.

    Sent as multipart/form-data: `author`, repeated `levels` fields, plus optional
    `name`, `description`, and `thumbnail`."""
    pack = await db.packs.find_one({"pack_id": pack_id, "deleted": {"$ne": True}})
    if not pack:
        raise HTTPException(404, "Pack not found")

    if pack["author"] != author:
        raise HTTPException(403, "You are not the author of this pack")

    levels = [code.strip() for code in levels if code.strip()]
    if len(levels) < MIN_PACK_LEVELS:
        raise HTTPException(400, f"A pack must contain at least {MIN_PACK_LEVELS} levels")

    now = datetime.now(timezone.utc)

    # Only the fields the caller actually supplied are written; anything omitted
    # is left untouched on the existing document.
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


@router.post("/{pack_id}/vote")
@limiter.limit("60/minute")
async def vote_pack(request: Request, pack_id: str, body: VoteRequest):
    """Set, change, or retract the caller's vote on a pack (spec §4.1).

    Body: `{ "gsid": string, "value": 1 | -1 | 0 }` — 1/-1 upsert the rating,
    0 retracts (deletes) it. The account is auto-created for an unseen gsid. The
    rating write and the pack's ups/downs/wilson update happen in one transaction,
    so it's safe under concurrent voters. Idempotent in effect: re-sending the same
    value yields the same final state.

    No API key required — this is a player action keyed on gsid (like time
    submission), not an admin operation. Response: `{ ups, downs, myVote }`, where
    `myVote` is the caller's vote *after* this write."""
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
    """Paged pack summaries enriched with rating data (spec §4.2).

    Each summary carries the existing fields plus `ups`, `downs`, `myVote`,
    `featured`, and `createdAt`. Counters are read straight off the denormalized
    pack fields (no aggregation over ratings), and `myVote` is a single keyed
    lookup over (pack_id, gsid) for the page — never a scan. Returns the envelope
    `{ packs, page, pageSize, total, hasMore }`."""
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
        # A pack's author is a discord_id today, so resolve the caller's gsid to
        # their paired discord_id and match on that. When pack authorship moves to
        # gsid, this becomes `base_match["author"] = gsid` and the lookup drops out.
        account = await db.accounts.find_one({"gsid": gsid}, {"_id": 0, "discord_id": 1})
        author_id = account.get("discord_id") if account else None
        if author_id is None:
            # Not paired yet -> no authored packs to show.
            return {"packs": [], "page": page, "pageSize": pageSize, "total": 0, "hasMore": False}
        base_match["author"] = author_id

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
        # Voted packs (n >= 1) by wilson desc; zero-vote packs last by created_at
        # desc. `_hasVotes` sorts the two groups apart (spec §3).
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
                "authorId": "$author",
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
        # One pass yields both the page and the unpaged total.
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
    """Single pack's current state. When `gsid` is supplied, the response also
    includes `ups`, `downs`, and the caller's `myVote` (spec §4.3). The common
    path gets vote state from the list summary, so this per-open fetch is a
    fallback."""
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
        "authorId": pack["author"],
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