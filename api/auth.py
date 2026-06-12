import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import aiohttp
from fastapi import HTTPException, Request

from .db import db

logger = logging.getLogger("lvlnet.auth")

# Session tokens are opaque 256-bit random hex strings stored server-side in
# `db.sessions`. The client refreshes 60s before `expiresIn`, so the stored TTL
# must be >= the advertised one — we keep them equal.
SESSION_TTL_SECONDS = 3600

STEAM_AUTH_URL = (
    "https://partner.steam-api.com/ISteamUserAuth/AuthenticateUserTicket/v1/"
)
DEFAULT_UCH_APP_ID = "426080"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime) -> datetime:
    """Mongo round-trips datetimes as naive UTC; normalize for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def verify_steam_ticket(ticket: str) -> str | None:
    """Verify a Steam auth session ticket with the Steam Web API.

    Returns the authenticated steamid string on success, None for an invalid /
    expired ticket. Raises 503 if the server has no publisher key configured and
    502 if Steam itself is unreachable (the client retries once on 5xx, never on
    4xx, so transport failures must not surface as 401)."""
    api_key = os.getenv("STEAM_WEB_API_KEY")
    if not api_key:
        raise HTTPException(503, "Steam authentication is not configured")

    params = {
        "key": api_key,
        "appid": os.getenv("STEAM_APP_ID", DEFAULT_UCH_APP_ID),
        "ticket": ticket,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(STEAM_AUTH_URL, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Steam ticket verification returned HTTP %s", resp.status
                    )
                    return None
                payload = await resp.json()
    except aiohttp.ClientError as exc:
        logger.error("Steam Web API unreachable: %s", exc)
        raise HTTPException(502, "Could not reach Steam to verify the ticket")

    result = (payload.get("response") or {}).get("params") or {}
    if result.get("result") != "OK":
        return None
    if result.get("vacbanned") or result.get("publisherbanned"):
        # Bans don't block map-pack auth, but they're worth a log line.
        logger.info("steamid %s authenticated with a ban flag", result.get("steamid"))
    return result.get("steamid")


async def bind_steamid(steamid: str, gsid: str) -> None:
    """Persist the steamid <-> gsid binding.

    Policy for a steamid already bound to a *different* gsid: update the binding
    to the new gsid and log it (gsids can be regenerated client-side)."""
    existing = await db.steam_bindings.find_one({"steamid": steamid})
    now = _utcnow()
    if existing and existing.get("gsid") != gsid:
        logger.warning(
            "steamid %s re-bound from gsid %s to %s",
            steamid,
            existing.get("gsid"),
            gsid,
        )
    await db.steam_bindings.update_one(
        {"steamid": steamid},
        {
            "$set": {"gsid": gsid, "updated_at": now},
            "$setOnInsert": {"steamid": steamid, "created_at": now},
        },
        upsert=True,
    )


async def create_session(gsid: str, client_version: str = "") -> str:
    """Mint an opaque session token bound to `gsid` with a 1h TTL."""
    token = secrets.token_hex(32)  # 256 bits
    now = _utcnow()
    await db.sessions.insert_one(
        {
            "token": token,
            "gsid": gsid,
            "client_version": client_version or "",
            "created_at": now,
            "expires_at": now + timedelta(seconds=SESSION_TTL_SECONDS),
        }
    )
    return token


async def require_session(request: Request) -> str:
    """FastAPI dependency: resolve `Authorization: Bearer <token>` to a gsid.

    Missing / unknown / expired token -> 401. The client invalidates its cached
    token on 401, re-authenticates exactly once, and retries the request, so
    enforcing expiry here is safe."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")

    session = await db.sessions.find_one({"token": token})
    if not session:
        raise HTTPException(401, "Invalid or expired session token")
    if _as_aware(session["expires_at"]) <= _utcnow():
        # Best-effort cleanup; the TTL index will catch anything we miss.
        await db.sessions.delete_one({"_id": session["_id"]})
        raise HTTPException(401, "Invalid or expired session token")

    return session["gsid"]


def enforce_gsid(token_gsid: str, supplied_gsid: str | None) -> str:
    """The token's gsid is authoritative. A supplied form/query gsid that
    disagrees with it is rejected with 401 and logged; an absent one is fine.
    Returns the authoritative gsid."""
    supplied = (supplied_gsid or "").strip()
    if supplied and supplied != token_gsid:
        logger.warning(
            "gsid mismatch: token gsid %s, supplied gsid %s", token_gsid, supplied
        )
        raise HTTPException(401, "gsid does not match the session token")
    return token_gsid