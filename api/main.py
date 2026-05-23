from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from api.routes.packs import router as packs_router
from api.routes.levels import router as levels_router
from api.routes.times import router as times_router
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
    except Exception:
        pass
    yield


app = FastAPI(title="LVLNet API", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(levels_router)
app.include_router(packs_router)
app.include_router(times_router)