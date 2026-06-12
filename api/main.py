from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from api.routes.auth import router as auth_router
from api.routes.drafts import router as drafts_router
from api.routes.packs import router as packs_router
from api.routes.levels import router as levels_router
from api.routes.times import router as times_router
from api.routes.versions import router as versions_router
from api.utils import limiter
from api.db import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Indexes for the leaderboard read/write paths. create_index is idempotent,
    # and we never block startup if Mongo is briefly unavailable.
    try:
        await db.times.create_index(
            [("pack_id", 1), ("version", 1), ("mode", 1), ("total_seconds", 1)],
            name="board_rank",
        )
        await db.times.create_index(
            [
                ("pack_id", 1),
                ("version", 1),
                ("mode", 1),
                ("gsid", 1),
                ("total_seconds", 1),
                ("created_at", 1),
            ],
            name="player_best",
        )
        await db.times.create_index(
            [("pack_id", 1), ("version", 1), ("mode", 1), ("deaths", 1), ("total_seconds", 1)],
            name="deathless_board",
        )

        # --- Ratings / accounts (pack voting) ---
        await db.ratings.create_index(
            [("pack_id", 1), ("gsid", 1)], name="rating_unique", unique=True
        )
        await db.accounts.create_index([("gsid", 1)], name="account_gsid", unique=True)

        # --- Auth sessions & Steam bindings ---
        # Token lookup on every authenticated request.
        await db.sessions.create_index([("token", 1)], name="session_token", unique=True)
        # TTL cleanup: Mongo reaps the row when expires_at passes. require_session
        # also checks expiry explicitly, so the lazy reaper's ~60s granularity
        # never lets an expired token through.
        await db.sessions.create_index(
            [("expires_at", 1)], name="session_ttl", expireAfterSeconds=0
        )
        # One credential record per gsid; the unique index also makes the
        # trust-on-first-use registration race-safe (insert wins exactly once).
        await db.client_secrets.create_index(
            [("gsid", 1)], name="client_secret_gsid", unique=True
        )

        # --- Drafts: author listing path ---
        await db.pack_drafts.create_index(
            [("author", 1), ("updated_at", -1)], name="drafts_by_author"
        )
        await db.pack_drafts.create_index([("pack_id", 1)], name="drafts_pack_id")

        # --- Pack list sort paths (spec §4.2). All filtered on non-deleted. ---
        await db.packs.create_index(
            [("deleted", 1), ("wilson", -1), ("created_at", -1)], name="packs_toprated"
        )
        await db.packs.create_index(
            [("deleted", 1), ("featured", -1), ("created_at", -1)], name="packs_featured"
        )
        await db.packs.create_index(
            [("deleted", 1), ("created_at", -1)], name="packs_newest"
        )
        await db.packs.create_index(
            [("deleted", 1), ("author", 1), ("created_at", -1)], name="packs_mylevels"
        )
    except Exception:
        pass
    yield


app = FastAPI(title="LVLNet API", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# /auth/steam must be reachable at exactly that path (no extra prefix): every
# route the client calls is unprefixed.
app.include_router(auth_router)
app.include_router(levels_router)
# drafts BEFORE packs: both live under /packs, and packs' GET /packs/{pack_id}
# would otherwise capture /packs/drafts.
app.include_router(drafts_router)
app.include_router(packs_router)
app.include_router(times_router)
app.include_router(versions_router)