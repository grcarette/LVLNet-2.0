import os
import logging

from fastapi import APIRouter, Request

from api.utils import limiter

logger = logging.getLogger("lvlnet.versions")

router = APIRouter(tags=["versions"])


@router.get("/client-version")
@limiter.limit("60/minute")
async def get_client_version(request: Request):
    """Returns the minimum client version required for leaderboard submissions.

    minSubmitVersion is read from MIN_SUBMIT_VERSION env var. If not set, an
    empty string is returned; the client treats an empty minSubmitVersion as
    unknown state and fails open (plays and submits normally)."""
    return {
        "minSubmitVersion": os.getenv("MIN_SUBMIT_VERSION", ""),
        "latest": os.getenv("LATEST_CLIENT_VERSION", ""),
        "updateUrl": os.getenv("CLIENT_UPDATE_URL", ""),
    }
