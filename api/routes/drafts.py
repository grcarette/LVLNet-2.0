from datetime import datetime, timezone
from typing import Optional, List

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Form,
    File,
    UploadFile,
    Response,
    Query,
)
from pymongo import ReturnDocument

from ..db import db
from api.utils import limiter
from .packs import (
    _process_thumbnail,
    _generate_unique_pack_id,
    MIN_PACK_LEVELS,
    ensure_account,
)

# Work-in-progress packs created from the game client. These live in their OWN
# collection (`pack_drafts`), never in `packs`. That separation is deliberate:
# a published pack is immutable (its levels are frozen because the leaderboard
# depends on the course never changing), while a draft's whole purpose is that
# you keep adding maps. Keeping them physically separate means a draft can never
# leak into a public listing or leaderboard, drafts get looser rules, and the
# real validation runs exactly once, at publish time.
#
# Authorship is the player's gsid (string), matching the keyless player model
# already used by /vote and /times. All routes here are KEYLESS and gsid-owned:
# the submitter must pass the gsid that authored the draft. (Per the project
# trust model the gsid is client-asserted and forgeable; accepted as negligible
# at this scale, same as votes/times.)
router = APIRouter(prefix="/packs/drafts", tags=["drafts"])


async def _get_draft_or_404(pack_id: str) -> dict:
    draft = await db.pack_drafts.find_one({"pack_id": pack_id})
    if not draft:
        raise HTTPException(404, "Draft not found")
    return draft


def _require_owner(draft: dict, gsid: str) -> None:
    if draft.get("author") != gsid:
        raise HTTPException(403, "You are not the author of this draft")


def _draft_summary(draft: dict) -> dict:
    return {
        "packId": draft["pack_id"],
        "status": "draft",
        "name": draft.get("name", ""),
        "levelCount": len(draft.get("levels", [])),
        "thumbnailUrl": (
            f"/packs/drafts/{draft['pack_id']}/thumbnail"
            if draft.get("thumbnail")
            else None
        ),
        "createdAt": draft["created_at"],
        "updatedAt": draft["updated_at"],
    }


# --------------------------------------------------------------------------- #
# Collection-level routes (no {pack_id}). Defined first for clarity; note the
# whole router is mounted BEFORE the packs router so GET /packs/drafts is not
# shadowed by GET /packs/{pack_id}.
# --------------------------------------------------------------------------- #

@router.post("")
@limiter.limit("30/minute")
async def create_draft(
    request: Request,
    gsid: str = Form(..., description="author gsid"),
    name: str = Form(""),
    description: str = Form(""),
    displayName: str = Form("", description="author display name to store/resolve"),
    levels: Optional[List[str]] = Form(None),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Start a work-in-progress pack authored by `gsid`. Everything except `gsid`
    is optional — a draft may begin with no name and no levels. Nothing here is
    validated against the publish rules; that happens later, at publish.

    The pack ID is generated now and is unique across BOTH `packs` and
    `pack_drafts`, so the ID (and any URL built from it) survives publishing
    unchanged. Sent as multipart/form-data."""
    gsid = gsid.strip()
    if not gsid:
        raise HTTPException(400, "gsid is required")

    name = name.strip()
    display_name = (displayName or "").strip()
    codes = [c.strip() for c in (levels or []) if c.strip()]

    now = datetime.now(timezone.utc)
    pack_id = await _generate_unique_pack_id()

    thumbnail_doc = None
    if thumbnail is not None:
        thumbnail_doc = await _process_thumbnail(thumbnail)

    await ensure_account(gsid, display_name or None)

    draft_doc = {
        "pack_id": pack_id,
        "author": gsid,
        "author_name": display_name or "",
        "name": name,
        "description": description,
        "thumbnail": thumbnail_doc,
        "levels": codes,
        "created_at": now,
        "updated_at": now,
    }
    await db.pack_drafts.insert_one(draft_doc)
    return {"packId": pack_id, "status": "draft", "levelCount": len(codes)}


@router.get("")
@limiter.limit("60/minute")
async def list_my_drafts(
    request: Request,
    gsid: str = Query(..., description="author gsid whose drafts to list"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=100),
):
    """List the drafts authored by `gsid` (the in-game "My Packs" view needs this
    to show drafts). Does NOT require Discord pairing. Empty list if none."""
    gsid = gsid.strip()
    if not gsid:
        raise HTTPException(400, "gsid is required")

    total = await db.pack_drafts.count_documents({"author": gsid})
    cursor = (
        db.pack_drafts.find({"author": gsid})
        .sort("updated_at", -1)
        .skip((page - 1) * pageSize)
        .limit(pageSize)
    )
    drafts = [_draft_summary(d) async for d in cursor]
    return {
        "drafts": drafts,
        "page": page,
        "pageSize": pageSize,
        "total": total,
        "hasMore": page * pageSize < total,
    }


# --------------------------------------------------------------------------- #
# Per-draft routes
# --------------------------------------------------------------------------- #

@router.get("/{pack_id}")
@limiter.limit("120/minute")
async def get_draft(
    request: Request,
    pack_id: str,
    gsid: str = Query(..., description="author gsid; drafts are private to their author"),
):
    """Fetch a single draft's detail (incl. its level codes), used to render the
    draft before publishing. Readable only by the authoring gsid."""
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, gsid.strip())

    thumbnail_url = (
        f"/packs/drafts/{pack_id}/thumbnail" if draft.get("thumbnail") else None
    )
    return {
        "packId": draft["pack_id"],
        "status": "draft",
        "name": draft.get("name", ""),
        "authorId": draft["author"],
        "author": draft.get("author_name") or "Player",
        "description": draft.get("description", ""),
        "thumbnailUrl": thumbnail_url,
        "levels": draft.get("levels", []),
        "createdAt": draft["created_at"],
        "updatedAt": draft["updated_at"],
    }


@router.post("/{pack_id}/levels")
@limiter.limit("60/minute")
async def add_level_to_draft(
    request: Request,
    pack_id: str,
    gsid: str = Form(..., description="author gsid"),
    code: str = Form(..., description="level code to append to the draft"),
):
    """Append a map (level code) to a draft. Order is preserved (it defines split
    order on the eventual leaderboard); a code already present is a no-op (the
    list is treated as a set). The submitter must be the draft's author.

    Sent as multipart/form-data: `gsid`, `code`."""
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, gsid.strip())

    code = code.strip()
    if not code:
        raise HTTPException(400, "Level code must not be empty")

    updated = await db.pack_drafts.find_one_and_update(
        {"pack_id": pack_id},
        {"$addToSet": {"levels": code}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    return {
        "packId": pack_id,
        "levels": updated.get("levels", []),
        "levelCount": len(updated.get("levels", [])),
    }


@router.delete("/{pack_id}/levels/{level_code}")
@limiter.limit("60/minute")
async def remove_level_from_draft(
    request: Request,
    pack_id: str,
    level_code: str,
    gsid: str = Form(..., description="author gsid"),
):
    """Remove a map from a draft by its code. The submitter must be the author.
    Sent as multipart/form-data: `gsid`."""
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, gsid.strip())

    updated = await db.pack_drafts.find_one_and_update(
        {"pack_id": pack_id},
        {"$pull": {"levels": level_code}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    return {
        "packId": pack_id,
        "levels": updated.get("levels", []),
        "levelCount": len(updated.get("levels", [])),
    }


@router.put("/{pack_id}")
@limiter.limit("30/minute")
async def update_draft(
    request: Request,
    pack_id: str,
    gsid: str = Form(..., description="author gsid"),
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    displayName: Optional[str] = Form(None),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Edit a draft's presentation (name / description / displayName / thumbnail).
    Unlike a published pack, a draft's `name` MAY be blank while you're still
    working — the non-blank requirement is only enforced at publish. Levels are
    not edited here; use the `/levels` endpoints. The submitter must be the
    author.

    Sent as multipart/form-data: `gsid`, plus any of `name`, `description`,
    `displayName`, `thumbnail`."""
    draft = await _get_draft_or_404(pack_id)
    gsid = gsid.strip()
    _require_owner(draft, gsid)

    set_fields = {"updated_at": datetime.now(timezone.utc)}
    if name is not None:
        set_fields["name"] = name.strip()
    if description is not None:
        set_fields["description"] = description
    if displayName is not None:
        dn = displayName.strip()
        set_fields["author_name"] = dn
        if dn:
            await ensure_account(gsid, dn)
    if thumbnail is not None:
        set_fields["thumbnail"] = await _process_thumbnail(thumbnail)

    await db.pack_drafts.update_one({"pack_id": pack_id}, {"$set": set_fields})
    return {"packId": pack_id, "status": "draft", "updated": True}


@router.delete("/{pack_id}")
@limiter.limit("30/minute")
async def delete_draft(
    request: Request, pack_id: str, gsid: str = Form(..., description="author gsid")
):
    """Hard-delete a draft. Drafts are private and have no leaderboard depending
    on them, so there is no soft-delete. The submitter must be the author.
    Sent as multipart/form-data: `gsid`."""
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, gsid.strip())

    await db.pack_drafts.delete_one({"pack_id": pack_id})
    return {"packId": pack_id, "deleted": True}


@router.post("/{pack_id}/publish")
@limiter.limit("30/minute")
async def publish_draft(
    request: Request, pack_id: str, gsid: str = Form(..., description="author gsid")
):
    """Promote a draft to a published, immutable pack: validate it, insert it into
    `packs` (levels frozen, `created_at` = publish time), and remove the draft.
    The resulting pack is authored by the gsid.

    Publishing enforces the same rules as direct creation: a non-blank name and
    at least MIN_PACK_LEVELS levels — otherwise the draft is left intact and a 400
    is returned. Per decision (§4-B), publishing does NOT change the visibility of
    any level the pack contains; an unlisted (hidden) level stays hidden in level
    listings. Pack playback resolves those levels regardless via the pack-scoped
    resolver GET /packs/{packId}/levels.

    The move is atomic against concurrent publishes: the draft is claimed with a
    single find-and-delete, and if validation fails the claimed draft is restored.
    Sent as multipart/form-data: `gsid`."""
    gsid = gsid.strip()

    # Atomically claim the draft so two concurrent publishes can't both proceed.
    claimed = await db.pack_drafts.find_one_and_delete({"pack_id": pack_id})
    if claimed is None:
        raise HTTPException(404, "Draft not found")

    if claimed.get("author") != gsid:
        await db.pack_drafts.insert_one(claimed)  # not ours — put it back
        raise HTTPException(403, "You are not the author of this draft")

    name = (claimed.get("name") or "").strip()
    levels = [c.strip() for c in claimed.get("levels", []) if c.strip()]

    if not name:
        await db.pack_drafts.insert_one(claimed)  # rollback
        raise HTTPException(400, "Pack name must not be empty before publishing")
    if len(levels) < MIN_PACK_LEVELS:
        await db.pack_drafts.insert_one(claimed)  # rollback
        raise HTTPException(
            400,
            f"A pack must contain at least {MIN_PACK_LEVELS} levels before publishing",
        )

    # Resolve a display name to store on the pack so reads never show
    # "Unknown User" for gsid-authored content.
    author_name = claimed.get("author_name")
    if not author_name:
        acct = await db.accounts.find_one(
            {"gsid": claimed["author"]}, {"_id": 0, "display_name": 1}
        )
        author_name = (acct or {}).get("display_name")
    author_name = author_name or "Player"

    now = datetime.now(timezone.utc)
    pack_doc = {
        "pack_id": claimed["pack_id"],
        "author": claimed["author"],   # gsid (string)
        "author_name": author_name,    # read-time display name for gsid authors
        "name": name,
        "description": claimed.get("description", ""),
        "thumbnail": claimed.get("thumbnail"),
        "levels": levels,
        "deleted": False,
        # Seed the denormalized rating aggregates, matching direct pack creation
        # so the toprated / featured sorts behave identically.
        "ups": 0,
        "downs": 0,
        "wilson": 0.0,
        "featured": False,
        "created_at": now,
        "updated_at": now,
    }
    await db.packs.insert_one(pack_doc)
    return {
        "packId": pack_doc["pack_id"],
        "status": "published",
        "levelCount": len(levels),
    }


@router.get("/{pack_id}/thumbnail")
@limiter.limit("120/minute")
async def get_draft_thumbnail(request: Request, pack_id: str):
    """Serve a draft's thumbnail. (Kept keyless and gsid-free so it can be used
    directly as an <img> src; thumbnails are not sensitive.)"""
    draft = await db.pack_drafts.find_one(
        {"pack_id": pack_id},
        {"_id": 0, "thumbnail": 1},
    )
    if not draft:
        raise HTTPException(404, "Draft not found")

    thumb = draft.get("thumbnail")
    if not thumb:
        raise HTTPException(404, "Thumbnail not found")

    return Response(
        content=bytes(thumb["data"]),
        media_type=thumb.get("content_type", "image/png"),
        headers={"X-Content-Type-Options": "nosniff"},
    )