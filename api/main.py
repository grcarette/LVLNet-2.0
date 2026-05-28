from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from api.routes.packs import router as packs_router
from api.routes.drafts import router as drafts_router
from api.routes.levels import router as levels_router
from api.routes.times import router as times_router
from api.routes.versions import router as versions_router
from api.utils import limiter
from api.db import db


async def _ensure_index(collection, keys, name):
    """Create an index, healing a legacy spec stored under the same name.

    create_index is a no-op when an index with this name and these exact keys
    already exists, so steady-state startups do nothing. If an index with this
    name exists with a *different* key pattern — e.g. an old leaderboard index
    that still included `version` — Mongo raises a conflict; we drop the stale
    index and recreate it with the new (versionless) spec. Any failure, including
    Mongo being briefly unavailable, is swallowed so startup is never blocked."""
    try:
        await collection.create_index(keys, name=name)
    except Exception:
        try:
            await collection.drop_index(name)
            await collection.create_index(keys, name=name)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Leaderboard read/write indexes. A board is keyed on (pack_id, mode) — packs
    # are immutable, so `version` is no longer part of a board's identity. The old
    # indexes (which led with pack_id then version) are self-healed: _ensure_index
    # drops and recreates any index found under these names with the legacy spec.
    await _ensure_index(
        db.times,
        [("pack_id", 1), ("mode", 1), ("total_seconds", 1)],
        "board_rank",
    )
    await _ensure_index(
        db.times,
        [("pack_id", 1), ("mode", 1), ("gsid", 1), ("total_seconds", 1), ("created_at", 1)],
        "player_best",
    )
    await _ensure_index(
        db.times,
        [("pack_id", 1), ("mode", 1), ("deaths", 1), ("total_seconds", 1)],
        "deathless_board",
    )
    # Author lookups for `GET /packs/by-author/{discord_id}`, which scans both
    # the published packs and the WIP drafts for one author.
    await _ensure_index(db.packs, [("author", 1)], "packs_author")
    await _ensure_index(db.pack_drafts, [("author", 1)], "drafts_author")
    yield


app = FastAPI(title="LVLNet API", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(levels_router)
# Mount drafts before packs so /packs/drafts/... is matched by the drafts router
# and never captured by the packs `/{pack_id}` parameter route.
app.include_router(drafts_router)
app.include_router(packs_router)
app.include_router(times_router)
app.include_router(versions_router)