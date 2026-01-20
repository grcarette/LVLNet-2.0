from fastapi import FastAPI
from api.routes.levels import router as levels_router

app = FastAPI(title="LVLNet API")

app.include_router(levels_router)