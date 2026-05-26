import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Query

from ..db import db
from api.utils import limiter
from api.models.time import (
    TimeSubmission,
    normalize_mode,
    DEFAULT_MODE,
    VALID_MODES,
)

logger = logging.getLogger("lvlnet.times")

router = APIRouter(prefix="/packs", tags=["times"])

# Sanity bounds for *ranked* times. A non-positive total is rejected outright;
# a total above the ceiling is still stored (history) but never ranked, so it
# can't poison a board with garbage/overflow values (open question #6).
MIN_RANKED_SECONDS = 0.0
MAX_RANKED_SECONDS = 24 * 60 * 60  # 24h of gameplay time — generous, adjustable.


def _best_per_player_pipeline(
    pack_id: str, version: int, mode: str, deathless_only: bool = False
) -> list:
    """Reduce the full submission history to one row per player (their best,
    earliest-on-tie) for a single board, sorted ready for ranking.

    Tiebreak (open question #5): equal totals -> earliest submission ranks higher.
    When deathless_only is True, only submissions where deaths == 0 are considered;
    rows with deaths == null (legacy clients) are excluded from the deathless board.
    """
    match: dict = {
        "pack_id": pack_id,
        "version": version,
        "mode": mode,
        "total_seconds": {
            "$gt": MIN_RANKED_SECONDS,
            "$lte": MAX_RANKED_SECONDS,
        },
    }
    if deathless_only:
        match["deaths"] = 0  # excludes null (legacy) and any deaths > 0

    return [
        {"$match": match},
        {"$sort": {"total_seconds": 1, "created_at": 1}},
        {
            "$group": {
                "_id": "$gsid",
                "gsid": {"$first": "$gsid"},
                "display_name": {"$first": "$display_name"},
                "total_seconds": {"$first": "$total_seconds"},
                "created_at": {"$first": "$created_at"},
                "deaths": {"$first": "$deaths"},
            }
        },
        # $group does not preserve order, so re-sort the collapsed rows.
        {"$sort": {"total_seconds": 1, "created_at": 1}},
    ]


async def _ranked_board(
    pack_id: str, version: int, mode: str, limit: Optional[int] = None,
    deathless_only: bool = False,
) -> list:
    """Full board as plain dicts with the exact camelCase keys the client wants.
    Ranks are 1-based and assigned over the complete ordering before any slice,
    so a `rank` returned on submit is correct even when the row is past `limit`.
    """
    rows = await db.times.aggregate(
        _best_per_player_pipeline(pack_id, version, mode, deathless_only)
    ).to_list(length=None)

    board = [
        {
            "rank": index,
            "displayName": row.get("display_name") or "",
            "gsid": row.get("gsid"),
            "totalSeconds": row.get("total_seconds"),
            "deaths": row.get("deaths"),
        }
        for index, row in enumerate(rows, start=1)
    ]
    return board if limit is None else board[:limit]


@router.post("/{pack_id}/times")
@limiter.limit("30/minute")
async def submit_time(request: Request, pack_id: str, submission: TimeSubmission):
    """Record a completion time. Full history is kept; the leaderboard is the
    best time per (gsid, packId, version, mode). Fire-and-forget for the client,
    but we return an advisory ack designed for future UI ("New PB! Rank 4")."""

    # --- Path is authoritative; body packId must agree if present. ---
    if submission.pack_id and submission.pack_id != pack_id:
        raise HTTPException(400, "packId in body does not match path")

    gsid = (submission.gsid or "").strip()
    if not pack_id or not gsid:
        raise HTTPException(400, "packId and gsid are required")

    mode = normalize_mode(submission.mode)
    if mode is None:
        raise HTTPException(400, f"mode must be one of {VALID_MODES}")

    total = submission.total_seconds
    if total is None or total <= MIN_RANKED_SECONDS:
        raise HTTPException(400, "totalSeconds must be greater than 0")

    # --- Best-effort pack/version lookup, only to sanity-log splits. We do NOT
    #     hard-fail on an unknown pack/version (brief: be liberal on submit). ---
    pack = await db.packs.find_one(
        {"pack_id": pack_id},
        {"_id": 0, "versions.version": 1, "versions.levels": 1},
    )
    version_doc = None
    if pack:
        version_doc = next(
            (v for v in pack.get("versions", []) if v.get("version") == submission.version),
            None,
        )
    if pack is None:
        logger.warning("time for unknown pack %s (storing anyway)", pack_id)
    elif version_doc is None:
        logger.warning(
            "time for unknown version %s of pack %s (storing anyway)",
            submission.version,
            pack_id,
        )
    else:
        expected = len(version_doc.get("levels", []))
        if expected and len(submission.splits) != expected:
            logger.warning(
                "splits length %d != level count %d for pack %s v%s",
                len(submission.splits),
                expected,
                pack_id,
                submission.version,
            )

    within_bounds = MIN_RANKED_SECONDS < total <= MAX_RANKED_SECONDS
    if not within_bounds:
        logger.warning(
            "totalSeconds %s outside ranked bounds for gsid %s on %s",
            total,
            gsid,
            pack_id,
        )

    # --- Previous best for this exact board, to decide newBest BEFORE inserting. ---
    prev_best = await db.times.find_one(
        {
            "pack_id": pack_id,
            "version": submission.version,
            "mode": mode,
            "gsid": gsid,
            "total_seconds": {"$gt": MIN_RANKED_SECONDS, "$lte": MAX_RANKED_SECONDS},
        },
        sort=[("total_seconds", 1), ("created_at", 1)],
        projection={"_id": 0, "total_seconds": 1},
    )
    new_best = within_bounds and (
        prev_best is None or total < prev_best["total_seconds"]
    )

    # --- Always store the submission (full history; never trust client time). ---
    now = datetime.now(timezone.utc)
    client_ip = request.client.host if request.client else None
    doc = {
        "pack_id": pack_id,
        "version": submission.version,
        "mode": mode,
        "gsid": gsid,
        "platform_id": submission.platform_id or "",
        "display_name": submission.display_name or "",
        "total_seconds": total,
        "splits": submission.splits,
        "created_at": now,
        "ip": client_ip,
        "deaths": submission.deaths,
        # Reserved for future Steam-ticket verification; never set true in v1.
        "verified": False,
        "ticket": submission.ticket,
    }
    await db.times.insert_one(doc)

    # --- Names change: keep the latest displayName/platformId on every row for
    #     this player so older best times still render the current name. ---
    await db.times.update_many(
        {
            "pack_id": pack_id,
            "version": submission.version,
            "mode": mode,
            "gsid": gsid,
        },
        {
            "$set": {
                "display_name": submission.display_name or "",
                "platform_id": submission.platform_id or "",
            }
        },
    )

    # --- Resolve current rank on the (updated) board. 0 = not ranked. ---
    rank = 0
    if within_bounds:
        board = await _ranked_board(pack_id, submission.version, mode)
        rank = next((e["rank"] for e in board if e["gsid"] == gsid), 0)

    return {"accepted": True, "newBest": new_best, "rank": rank}


@router.get("/{pack_id}/times")
@limiter.limit("120/minute")
async def get_leaderboard(
    request: Request,
    pack_id: str,
    version: int = Query(1),
    limit: int = Query(50, ge=1, le=500),
    mode: Optional[str] = Query(None),
    deathless: bool = Query(False),
):
    """Leaderboard for one board, ascending by totalSeconds, capped at `limit`.
    Omitted `mode` -> Speedrun. Returns an object with an `entries` array
    (Unity JsonUtility cannot parse a bare top-level array).
    Pass `deathless=true` to return only submissions where deaths == 0."""
    resolved_mode = normalize_mode(mode) if mode else DEFAULT_MODE
    if resolved_mode is None:
        raise HTTPException(400, f"mode must be one of {VALID_MODES}")

    entries = await _ranked_board(pack_id, version, resolved_mode, limit=limit, deathless_only=deathless)
    return {"entries": entries}


@router.get("/{pack_id}/times/{gsid}")
@limiter.limit("120/minute")
async def get_player_splits(
    request: Request,
    pack_id: str,
    gsid: str,
    version: int = Query(1),
    mode: Optional[str] = Query(None),
):
    """Per-row detail for a future "expand a leaderboard row" UI (open question
    #8): the player's best time on this board, with stored cumulative splits."""
    resolved_mode = normalize_mode(mode) if mode else DEFAULT_MODE
    if resolved_mode is None:
        raise HTTPException(400, f"mode must be one of {VALID_MODES}")

    best = await db.times.find_one(
        {
            "pack_id": pack_id,
            "version": version,
            "mode": resolved_mode,
            "gsid": gsid,
            "total_seconds": {"$gt": MIN_RANKED_SECONDS, "$lte": MAX_RANKED_SECONDS},
        },
        sort=[("total_seconds", 1), ("created_at", 1)],
        projection={"_id": 0},
    )
    if not best:
        raise HTTPException(404, "No time found for that player on this board")

    board = await _ranked_board(pack_id, version, resolved_mode)
    rank = next((e["rank"] for e in board if e["gsid"] == gsid), 0)

    return {
        "packId": pack_id,
        "version": version,
        "mode": resolved_mode,
        "rank": rank,
        "gsid": gsid,
        "displayName": best.get("display_name") or "",
        "totalSeconds": best.get("total_seconds"),
        "splits": best.get("splits", []),
    }