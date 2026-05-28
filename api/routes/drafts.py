from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Request, Form, File, UploadFile, Response, Depends
from pymongo import ReturnDocument

from ..db import db
from api.utils import limiter, require_api_key
from .packs import _process_thumbnail, _generate_unique_pack_id, MIN_PACK_LEVELS

# Work-in-progress packs. These live in their OWN collection (`pack_drafts`),
# never in `packs`. That is deliberate: a published pack is immutable (its levels
# are frozen because the leaderboard depends on the course never changing), while
# a draft is the opposite — its whole purpose is that you keep adding maps. Keeping
# them physically separate means:
#   * a draft can never leak into a public listing or a leaderboard, because it
#     simply isn't in the collection those endpoints query — no `is_wip` filter to
#     remember in six places;
#   * drafts get looser rules (no name yet, fewer than the publish minimum of
#     levels) and the real validation runs exactly once, at publish time;
#   * there is no code path anywhere that mutates a published pack's levels.
#
# Everything here requires the API key. Drafts are private; they should not be
# enumerable or readable by the public.
router = APIRouter(prefix="/packs/drafts", tags=["drafts"])


async def _get_draft_or_404(pack_id: str) -> dict:
    draft = await db.pack_drafts.find_one({"pack_id": pack_id})
    if not draft:
        raise HTTPException(404, "Draft not found")
    return draft


def _require_owner(draft: dict, author: int) -> None:
    if draft["author"] != author:
        raise HTTPException(403, "You are not the author of this draft")


@router.post("", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def create_draft(
    request: Request,
    author: int = Form(...),
    name: str = Form(""),
    description: str = Form(""),
    levels: Optional[List[str]] = Form(None),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Start a work-in-progress pack. Everything except `author` is optional — a
    draft may begin with no name and no levels. Nothing here is validated against
    the publish rules; that happens later, at `POST .../publish`.

    The pack ID is generated now and is unique across BOTH `packs` and
    `pack_drafts`, so the ID (and any URL built from it) survives publishing
    unchanged. Sent as multipart/form-data."""
    name = name.strip()
    codes = [c.strip() for c in (levels or []) if c.strip()]

    now = datetime.now(timezone.utc)
    pack_id = await _generate_unique_pack_id()

    thumbnail_doc = None
    if thumbnail is not None:
        thumbnail_doc = await _process_thumbnail(thumbnail)

    draft_doc = {
        "pack_id": pack_id,
        "author": author,
        "name": name,
        "description": description,
        "thumbnail": thumbnail_doc,
        "levels": codes,
        "created_at": now,
        "updated_at": now,
    }
    await db.pack_drafts.insert_one(draft_doc)
    return {"packId": pack_id, "status": "draft", "levelCount": len(codes)}


@router.get("/{pack_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute")
async def get_draft(request: Request, pack_id: str):
    """Fetch a single draft (API key required — drafts are private)."""
    draft = await _get_draft_or_404(pack_id)

    author = await db.users.find_one({"discord_id": draft["author"]})
    author_name = author["username"] if author else "Unknown User"

    thumbnail_url = None
    if draft.get("thumbnail"):
        thumbnail_url = f"/packs/drafts/{pack_id}/thumbnail"

    return {
        "packId": draft["pack_id"],
        "status": "draft",
        "name": draft.get("name", ""),
        "authorId": draft["author"],
        "author": author_name,
        "description": draft.get("description", ""),
        "thumbnailUrl": thumbnail_url,
        "levels": draft.get("levels", []),
        "createdAt": draft["created_at"],
        "updatedAt": draft["updated_at"],
    }


@router.post("/{pack_id}/levels", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute")
async def add_level_to_draft(
    request: Request,
    pack_id: str,
    author: int = Form(...),
    code: str = Form(..., description="level code to append to the draft"),
):
    """Append a map (level code) to a draft. Order is preserved (it defines split
    order on the eventual leaderboard); a code already present is a no-op (the list
    is treated as a set). The submitter must be the draft's author.

    Sent as multipart/form-data: `author`, `code`."""
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, author)

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


@router.delete("/{pack_id}/levels/{level_code}", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute")
async def remove_level_from_draft(
    request: Request,
    pack_id: str,
    level_code: str,
    author: int = Form(...),
):
    """Remove a map from a draft by its code. The submitter must be the author.
    Sent as multipart/form-data: `author`."""
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, author)

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


@router.put("/{pack_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def update_draft(
    request: Request,
    pack_id: str,
    author: int = Form(...),
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Edit a draft's presentation (name / description / thumbnail). Unlike a
    published pack, a draft's `name` MAY be blank while you're still working — the
    non-blank requirement is only enforced at publish. Levels are not edited here;
    use the `/levels` endpoints. The submitter must be the author.

    Sent as multipart/form-data: `author`, plus any of `name`, `description`,
    `thumbnail`."""
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, author)

    set_fields = {"updated_at": datetime.now(timezone.utc)}
    if name is not None:
        set_fields["name"] = name.strip()
    if description is not None:
        set_fields["description"] = description
    if thumbnail is not None:
        set_fields["thumbnail"] = await _process_thumbnail(thumbnail)

    await db.pack_drafts.update_one({"pack_id": pack_id}, {"$set": set_fields})
    return {"packId": pack_id, "status": "draft", "updated": True}


@router.delete("/{pack_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def delete_draft(request: Request, pack_id: str, author: int = Form(...)):
    """Hard-delete a draft. Drafts are private and have no leaderboard depending
    on them, so there is no soft-delete. The submitter must be the author.
    Sent as multipart/form-data: `author`."""
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, author)

    await db.pack_drafts.delete_one({"pack_id": pack_id})
    return {"packId": pack_id, "deleted": True}


@router.post("/{pack_id}/publish", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def publish_draft(request: Request, pack_id: str, author: int = Form(...)):
    """Promote a draft to a published, immutable pack: validate it, insert it into
    `packs` (levels frozen, `created_at` = publish time), and remove the draft.

    The submitter must be the author. Publishing enforces the same rules as direct
    creation: a non-blank name and at least MIN_PACK_LEVELS levels — otherwise the
    draft is left intact and a 400 is returned.

    The move is atomic against concurrent publishes: the draft is claimed with a
    single find-and-delete, and if validation fails the claimed draft is restored.
    Sent as multipart/form-data: `author`."""
    # Atomically claim the draft so two concurrent publishes can't both proceed.
    claimed = await db.pack_drafts.find_one_and_delete({"pack_id": pack_id})
    if claimed is None:
        raise HTTPException(404, "Draft not found")

    if claimed["author"] != author:
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
            400, f"A pack must contain at least {MIN_PACK_LEVELS} levels before publishing"
        )

    now = datetime.now(timezone.utc)
    pack_doc = {
        "pack_id": claimed["pack_id"],
        "author": claimed["author"],
        "name": name,
        "description": claimed.get("description", ""),
        "thumbnail": claimed.get("thumbnail"),
        "levels": levels,
        "deleted": False,
        "created_at": now,
        "updated_at": now,
    }
    await db.packs.insert_one(pack_doc)
    return {"packId": pack_doc["pack_id"], "status": "published", "levelCount": len(levels)}


@router.get("/{pack_id}/thumbnail", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute")
async def get_draft_thumbnail(request: Request, pack_id: str):
    """Serve a draft's thumbnail (API key required — drafts are private)."""
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