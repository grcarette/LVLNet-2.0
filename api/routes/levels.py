from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Body,
    Depends,
    Form,
    File,
    UploadFile,
    Response,
)
from typing import List, Optional

from ..db import db
from ..imgur import get_imgur_data
from ..level_projection import creator_lookup_stages
from api.models.level import LevelCreateRequest
from api.utils import limiter, require_api_key
# ensure_account / _process_thumbnail live in the packs router. Importing them
# here is safe (packs does not import levels, so there is no cycle) and reuses
# the exact same account-seeding and thumbnail validate/downscale/PNG pipeline.
from .packs import ensure_account, _process_thumbnail

router = APIRouter(prefix="/levels", tags=["levels"])

VALID_MODES = ("party", "challenge")


def _is_valid_level_code(code: str) -> bool:
    return len(code) == 9 and code[4] == "-"


# --------------------------------------------------------------------------- #
# Reads. All share the same creator/thumbnail projection; only the $match in
# front differs. The discovery endpoints (batch / list / random) keep excluding
# hidden levels exactly as before.
# --------------------------------------------------------------------------- #

@router.post("/batch")
@limiter.limit("60/minute")
async def get_levels_from_list(request: Request, codes: List[str] = Body(...)):
    # Unchanged behaviour: hidden levels are excluded from this discovery-style
    # batch lookup. (Pack playback that must include hidden levels uses the
    # pack-scoped resolver GET /packs/{packId}/levels instead.)
    pipeline = [
        {"$match": {"code": {"$in": codes}, "hidden": {"$ne": True}}},
        *creator_lookup_stages(),
    ]
    return await db.levels.aggregate(pipeline).to_list(length=len(codes))


@router.get("/by-user/{gsid}")
@limiter.limit("60/minute")
async def get_levels_by_user(request: Request, gsid: str):
    """All levels authored by a given gsid, INCLUDING hidden (unlisted) ones.

    This is the owner's own view of their uploads (e.g. the in-game "my levels"
    list, and resolving the codes inside their own draft packs). Hidden gates
    discovery for *other* people, not the author seeing their own content.

    Per the project trust model the gsid is client-asserted and forgeable; this
    endpoint is keyed on it by convention, the same stance already taken for
    votes and time submissions.
    """
    gsid = gsid.strip()
    if not gsid:
        raise HTTPException(400, "gsid is required")
    pipeline = [
        {"$match": {"author_gsid": gsid}},
        *creator_lookup_stages(),
    ]
    return await db.levels.aggregate(pipeline).to_list(length=None)


@router.get("/{code}")
@limiter.limit("120/minute")
async def get_level(request: Request, code: str):
    pipeline = [
        {"$match": {"code": code}},
        *creator_lookup_stages(),
    ]
    results = await db.levels.aggregate(pipeline).to_list(length=1)
    if not results:
        raise HTTPException(404, "Level not found")
    return results[0]


@router.get("/{code}/thumbnail")
@limiter.limit("120/minute")
async def get_level_thumbnail(request: Request, code: str):
    """Serve a game-created level's stored thumbnail. Imgur-sourced levels have no
    stored thumbnail (they use `imgur_url`); those return 404 here."""
    level = await db.levels.find_one({"code": code}, {"_id": 0, "thumbnail": 1})
    if not level or not level.get("thumbnail"):
        raise HTTPException(404, "Thumbnail not found")
    thumb = level["thumbnail"]
    return Response(
        content=bytes(thumb["data"]),
        media_type=thumb.get("content_type", "image/png"),
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/random/{amount}")
@limiter.limit("120/minute")
async def get_random_levels(request: Request, amount: int):
    amount = max(1, min(amount, 5))
    pipeline = [
        {"$match": {"tournament_legal": True, "hidden": {"$ne": True}}},
        {"$sample": {"size": amount}},
        *creator_lookup_stages(),
    ]
    return await db.levels.aggregate(pipeline).to_list(length=amount)


@router.get("/")
@limiter.limit("30/minute")
async def list_levels(
    request: Request,
    tournament_legal: bool | None = None,
    mode: str = "party",
):
    query = {"mode": mode.lower(), "hidden": {"$ne": True}}
    if tournament_legal is not None:
        query["tournament_legal"] = True

    pipeline = [
        {"$match": query},
        *creator_lookup_stages(),
    ]
    return await db.levels.aggregate(pipeline).to_list(length=None)


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

VALID_MODES_TUP = VALID_MODES  # alias kept for readability below


async def _upload_one(body: LevelCreateRequest):
    """Resolve, validate, and write a single level FROM AN IMGUR LINK.
    Returns (result, None) on success or (None, (status, reason)) on failure.

    This is the existing Discord-bot / admin path: it reads the level code from
    the Imgur description and is gated by an API key at the route. Unchanged."""
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


@router.post("/from-game")
@limiter.limit("30/minute")
async def create_level_from_game(
    request: Request,
    code: str = Form(..., description="9-char XXXX-YYYY level code"),
    name: str = Form(...),
    gsid: str = Form(..., description="author (game player) gsid"),
    mode: str = Form(..., description="party | challenge"),
    displayName: str = Form("", description="author display name to store/resolve"),
    hidden: bool = Form(False, description="true when the player uploaded 'unlisted'"),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Register a level created in-game by code + gsid (no Imgur, no API key).

    The game already has the level code and (optionally) a thumbnail, so unlike
    POST /levels/ this takes them directly. The level is authored by `gsid` and
    carries the player's display name for read-time resolution.

    Idempotent re-register: if the code already exists AND is authored by the
    same gsid, this updates it (name / mode / hidden / thumbnail) instead of
    409ing — so flipping unlisted<->public or fixing the name just works. A code
    owned by a *different* author returns 409.

    Sent as multipart/form-data (an optional thumbnail file may be attached).
    """
    code = code.strip().upper()
    if not _is_valid_level_code(code):
        raise HTTPException(400, "code must be a 9-char XXXX-YYYY level code")

    mode_l = mode.strip().lower()
    if mode_l not in VALID_MODES:
        raise HTTPException(400, f"mode must be one of {VALID_MODES}")

    name = name.strip()
    if not name:
        raise HTTPException(400, "name must not be empty")

    gsid = gsid.strip()
    if not gsid:
        raise HTTPException(400, "gsid is required")

    display_name = (displayName or "").strip()

    thumbnail_doc = None
    if thumbnail is not None:
        thumbnail_doc = await _process_thumbnail(thumbnail)

    # Auto-register the account on first contact; refresh the display name.
    await ensure_account(gsid, display_name or None)

    existing = await db.levels.find_one({"code": code})
    if existing is not None:
        if existing.get("author_gsid") != gsid:
            # Owned by someone else (or a discord-authored level). Don't clobber.
            raise HTTPException(409, "A level with that code already exists")

        update = {"name": name, "mode": mode_l, "hidden": hidden}
        if display_name:
            update["author_name"] = display_name
        if thumbnail_doc is not None:
            update["thumbnail"] = thumbnail_doc
        await db.levels.update_one({"code": code}, {"$set": update})
        return {"code": code, "hidden": hidden, "created": False}

    level_doc = {
        "code": code,
        "name": name,
        "mode": mode_l,
        "imgur_url": "",            # game levels have no Imgur source
        "creators": [],             # no discord creators
        "author_gsid": gsid,        # gsid authorship
        "author_name": display_name or "",
        "tournament_legal": False,  # arbiters set legality later, as today
        "hidden": hidden,
        "thumbnail": thumbnail_doc,
    }
    await db.levels.insert_one(level_doc)
    return {"code": code, "hidden": hidden, "created": True}