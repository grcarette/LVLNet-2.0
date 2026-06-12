import logging

from fastapi import APIRouter, Form, HTTPException, Request

from api.utils import limiter
from ..auth import (
    SESSION_TTL_SECONDS,
    create_session,
    register_or_verify_secret,
)
from .packs import ensure_account

logger = logging.getLogger("lvlnet.auth")

# Mounted with NO extra app-level prefix: the client calls exactly POST
# /auth/login (every route it uses is unprefixed, e.g. /packs/...).
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
@limiter.limit("20/minute")
async def login(
    request: Request,
    gsid: str = Form(...),
    secret: str = Form(...),
    clientVersion: str = Form(""),
):
    """Exchange the mod's (gsid, secret) credential pair for an opaque session
    token. Trust-on-first-use: the first login ever seen for a gsid registers
    the secret; every later login must present the same secret.

    Sent as application/x-www-form-urlencoded (Unity WWWForm):
    `gsid`, `secret` (client-generated 64-char hex), `clientVersion`.

    Responses:
      200 -> {"token": "<opaque>", "expiresIn": 3600, "registered": bool}
      400 -> missing/malformed fields (permanent; client must not retry)
      401 -> secret does not match the registered one (permanent)
      5xx -> transient; client may retry once
    """
    gsid = gsid.strip()
    secret = secret.strip()
    if not gsid or not secret:
        raise HTTPException(400, "gsid and secret are required")

    registered = await register_or_verify_secret(gsid, secret)
    await ensure_account(gsid)

    token = await create_session(gsid, clientVersion.strip())
    logger.info(
        "issued session for gsid %s (%s)",
        gsid,
        "first registration" if registered else "verified",
    )
    return {"token": token, "expiresIn": SESSION_TTL_SECONDS, "registered": registered}


@router.post("/steam")
async def steam_auth_gone(request: Request):
    """The Steam-ticket flow was removed (it requires a publisher Web API key
    for UCH's appid, which only the game's developer can hold). Old clients
    land here; 410 tells them unambiguously this is not transient."""
    raise HTTPException(410, "Steam auth has been replaced by POST /auth/login")