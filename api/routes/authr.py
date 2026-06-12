import logging

from fastapi import APIRouter, Form, HTTPException, Request

from api.utils import limiter
from ..auth import (
    SESSION_TTL_SECONDS,
    bind_steamid,
    create_session,
    verify_steam_ticket,
)
from .packs import ensure_account

logger = logging.getLogger("lvlnet.auth")

# Mounted with NO extra app-level prefix: the client calls exactly POST
# /auth/steam (every route it uses is unprefixed, e.g. /packs/...).
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/steam")
@limiter.limit("30/minute")
async def steam_auth(
    request: Request,
    gsid: str = Form(...),
    ticket: str = Form(...),
    clientVersion: str = Form(""),
):
    """Exchange a Steam auth session ticket for an opaque session token.

    Sent by the Unity client as application/x-www-form-urlencoded (WWWForm):
    `gsid`, `ticket` (lowercase hex), `clientVersion`.

    Response: `{"token": "<opaque>", "expiresIn": 3600}`. The client treats a
    missing `token` as failure, retries once only on network errors / 5xx, and
    never retries 4xx."""
    gsid = gsid.strip()
    ticket = ticket.strip()
    if not gsid or not ticket:
        raise HTTPException(400, "gsid and ticket are required")

    steamid = await verify_steam_ticket(ticket)
    if not steamid:
        raise HTTPException(401, "Invalid or expired Steam ticket")

    await bind_steamid(steamid, gsid)
    await ensure_account(gsid)

    token = await create_session(gsid, clientVersion.strip())
    logger.info("issued session for gsid %s (steamid %s)", gsid, steamid)
    return {"token": token, "expiresIn": SESSION_TTL_SECONDS}