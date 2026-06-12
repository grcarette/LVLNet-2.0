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
    Depends,
    Query,
)
from pymongo import ReturnDocument

from ..db import db
from ..auth import require_session, enforce_gsid
from api.utils import limiter
from .packs import _process_thumbnail, _generate_unique_pack_id, MIN_PACK_LEVELS, _resolve_author_name, update_display_name

# Work-in-progress packs. These live in their OWN collection (`pack_drafts`),
# never in `packs`. That is deliberate: a published pack is immutable (its levels
# are frozen because the leaderboard depends on the course never changing), while
# a draft is the opposite — its whole purpose is that you keep adding maps.
#
# Identity: drafts are owned by a gsid (the `author` field). Every route here
# requires a valid `Authorization: Bearer <token>` session (see api/auth.py);
# the token's gsid is authoritative, and any `gsid` form/query field the client
# also sends must agree with it or the request is rejected with 401.
#
# A draft may also be a previously-published pack that the author unpublished
# (POST /packs/{id}/unpublish). Such drafts carry extra bookkeeping fields:
#   * published_created_at — the original publish timestamp, restored on
#     re-publish so the pack keeps its age;
#   * published_levels — snapshot of the level list as it was on the public
#     board. If the list changes before re-publish, the leaderboard is wiped at
#     publish time (stored totals are no longer comparable);
#   * published_ratings — {ups, downs, wilson, featured}, restored on re-publish
#     (votes are retained, merely hidden, while unpublished).
router = APIRouter(prefix="/packs/drafts", tags=["drafts"])


def _draft_summary(draft: dict) -> dict:
    """Summary shape shared by the draft list — same keys the client already
    renders for ordinary drafts (packId, name, levelCount)."""
    return {
        "packId": draft["pack_id"],
        "name": draft.get("name", ""),
        "levelCount": len(draft.get("levels", [])),
    }


async def _get_draft_or_404(pack_id: str) -> dict:
    draft = await db.pack_drafts.find_one({"pack_id": pack_id})
    if not draft:
        raise HTTPException(404, "Draft not found")
    return draft


def _require_owner(draft: dict, gsid: str) -> None:
    if str(draft["author"]) != gsid:
        raise HTTPException(403, "You are not the author of this draft")


@router.post("")
@limiter.limit("30/minute")
async def create_draft(
    request: Request,
    token_gsid: str = Depends(require_session),
    gsid: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    levels: Optional[List[str]] = Form(None),
    thumbnail: Optional[UploadFile] = File(None),
    displayName: str = Form(""),
):
    """Start a work-in-progress pack owned by the authenticated gsid. Everything
    is optional — a draft may begin with no name and no levels. The publish rules
    are enforced later, at `POST .../publish`.

    The pack ID is generated now and is unique across BOTH `packs` and
    `pack_drafts`, so the ID (and any URL built from it) survives publishing
    unchanged. Sent as multipart/form-data."""
    author = enforce_gsid(token_gsid, gsid)
    await update_display_name(author, displayName)

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
        "author_display_name": displayName.strip() or None,
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
@limiter.limit("120/minute")
async def list_drafts(
    request: Request,
    token_gsid: str = Depends(require_session),
    gsid: Optional[str] = Query(None),
):
    """List the authenticated author's drafts (including unpublished packs),
    newest-updated first. Each entry uses the standard draft summary shape:
    `{packId, name, levelCount}`. Returned inside an object envelope because
    Unity's JsonUtility cannot parse a bare top-level array."""
    author = enforce_gsid(token_gsid, gsid)

    cursor = db.pack_drafts.find({"author": author}).sort("updated_at", -1)
    drafts = [_draft_summary(d) async for d in cursor]
    return {"drafts": drafts}


@router.get("/{pack_id}")
@limiter.limit("120/minute")
async def get_draft(
    request: Request,
    pack_id: str,
    token_gsid: str = Depends(require_session),
    gsid: Optional[str] = Query(None),
):
    """Fetch a single draft. Drafts are private: only the author may read one."""
    caller = enforce_gsid(token_gsid, gsid)
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, caller)

    thumbnail_url = None
    if draft.get("thumbnail"):
        thumbnail_url = f"/packs/drafts/{pack_id}/thumbnail"

    author_name = await _resolve_author_name(draft["author"], draft.get("author_display_name"))

    return {
        "packId": draft["pack_id"],
        "status": "draft",
        "name": draft.get("name", ""),
        "authorId": str(draft["author"]),
        "author": author_name,
        "description": draft.get("description", ""),
        "thumbnailUrl": thumbnail_url,
        "levels": draft.get("levels", []),
        "levelCount": len(draft.get("levels", [])),
        "createdAt": draft["created_at"],
        "updatedAt": draft["updated_at"],
    }


@router.post("/{pack_id}/levels")
@limiter.limit("60/minute")
async def add_level_to_draft(
    request: Request,
    pack_id: str,
    token_gsid: str = Depends(require_session),
    gsid: str = Form(""),
    code: str = Form(..., description="level code to append to the draft"),
):
    """Append a map (level code) to a draft. Order is preserved (it defines split
    order on the eventual leaderboard); a code already present is a no-op (the
    list is treated as a set). Only the draft's author may do this."""
    caller = enforce_gsid(token_gsid, gsid)
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, caller)

    code = code.strip()
    if not code:
        raise HTTPException(400, "Level code must not be empty")

    updated = await db.pack_drafts.find_one_and_update(
        {"pack_id": pack_id},
        {
            "$addToSet": {"levels": code},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
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
    token_gsid: str = Depends(require_session),
    gsid: str = Form(""),
):
    """Remove a map from a draft by its code. Only the author may do this."""
    caller = enforce_gsid(token_gsid, gsid)
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, caller)

    updated = await db.pack_drafts.find_one_and_update(
        {"pack_id": pack_id},
        {
            "$pull": {"levels": level_code},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
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
    token_gsid: str = Depends(require_session),
    gsid: str = Form(""),
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    thumbnail: Optional[UploadFile] = File(None),
):
    """Edit a draft's presentation (name / description / thumbnail). A draft's
    `name` MAY be blank while you're still working — the non-blank requirement is
    only enforced at publish. Levels are not edited here; use the `/levels`
    endpoints. Only the author may do this."""
    caller = enforce_gsid(token_gsid, gsid)
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, caller)

    set_fields = {"updated_at": datetime.now(timezone.utc)}
    if name is not None:
        set_fields["name"] = name.strip()
    if description is not None:
        set_fields["description"] = description
    if thumbnail is not None:
        set_fields["thumbnail"] = await _process_thumbnail(thumbnail)

    await db.pack_drafts.update_one({"pack_id": pack_id}, {"$set": set_fields})
    return {"packId": pack_id, "status": "draft", "updated": True}


@router.delete("/{pack_id}")
@limiter.limit("30/minute")
async def delete_draft(
    request: Request,
    pack_id: str,
    token_gsid: str = Depends(require_session),
    gsid: str = Form(""),
):
    """Hard-delete a draft. Drafts are private and have no leaderboard depending
    on them, so there is no soft-delete. Only the author may do this."""
    caller = enforce_gsid(token_gsid, gsid)
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, caller)

    await db.pack_drafts.delete_one({"pack_id": pack_id})
    return {"packId": pack_id, "deleted": True}


@router.post("/{pack_id}/publish")
@limiter.limit("30/minute")
async def publish_draft(
    request: Request,
    pack_id: str,
    token_gsid: str = Depends(require_session),
    gsid: str = Form(""),
):
    """Promote a draft to a published, immutable pack: validate it, insert it
    into `packs` (levels frozen), and remove the draft.

    Re-publish path (draft created by unpublish): the pack keeps the SAME packId
    — the id was generated once and is unique across both collections, and the
    original `packs` row was removed at unpublish time, so no uniqueness check
    can collide. The original created_at and rating counters are restored. If
    the level list (count or order) changed while unpublished, the pack's
    leaderboard is wiped here, because stored totals are no longer comparable;
    if the list is unchanged, existing times become visible again untouched.

    The move is atomic against concurrent publishes: the draft is claimed with a
    single find-and-delete, and if validation fails the claimed draft is
    restored."""
    caller = enforce_gsid(token_gsid, gsid)

    # Atomically claim the draft so two concurrent publishes can't both proceed.
    claimed = await db.pack_drafts.find_one_and_delete({"pack_id": pack_id})
    if claimed is None:
        raise HTTPException(404, "Draft not found")

    if str(claimed["author"]) != caller:
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

    now = datetime.now(timezone.utc)
    ratings = claimed.get("published_ratings") or {}
    previous_levels = claimed.get("published_levels")

    pack_doc = {
        "pack_id": claimed["pack_id"],
        "author": claimed["author"],
        "author_display_name": claimed.get("author_display_name"),
        "name": name,
        "description": claimed.get("description", ""),
        "thumbnail": claimed.get("thumbnail"),
        "levels": levels,
        "deleted": False,
        "ups": ratings.get("ups", 0),
        "downs": ratings.get("downs", 0),
        "wilson": ratings.get("wilson", 0.0),
        "featured": ratings.get("featured", False),
        # First publish: now. Re-publish: the original publish timestamp.
        "created_at": claimed.get("published_created_at") or now,
        "updated_at": now,
    }
    await db.packs.insert_one(pack_doc)

    # Leaderboard validity across an unpublish/re-publish cycle.
    if previous_levels is not None and previous_levels != levels:
        await db.times.delete_many({"pack_id": claimed["pack_id"]})

    return {
        "packId": pack_doc["pack_id"],
        "status": "published",
        "levelCount": len(levels),
    }


@router.get("/{pack_id}/thumbnail")
@limiter.limit("120/minute")
async def get_draft_thumbnail(
    request: Request,
    pack_id: str,
    token_gsid: str = Depends(require_session),
):
    """Serve a draft's thumbnail (drafts are private: author only)."""
    draft = await _get_draft_or_404(pack_id)
    _require_owner(draft, token_gsid)

    thumb = draft.get("thumbnail")
    if not thumb:
        raise HTTPException(404, "Thumbnail not found")

    return Response(
        content=bytes(thumb["data"]),
        media_type=thumb.get("content_type", "image/png"),
        headers={"X-Content-Type-Options": "nosniff"},
    )