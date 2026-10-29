"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import require_admin
from app.api.routes import auth, clips, publish, videos
from app.core import auth as auth_core
from app.core.config import settings
from app.core.logging import logger, setup_logging
from app.db.schemas import HealthResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    auth_core.check_secret_key()
    if not auth_core.auth_required():
        logger.warning("ADMIN_PASSWORD is not set: the API has no login. Fine on localhost, never on a public URL.")
    logger.info("Snipster API starting up...")
    yield
    logger.info("Snipster API shutting down.")


app = FastAPI(
    title="Snipster API",
    description="YouTube → Shorts/Reels repurposing tool",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow the Vite dev server and any configured frontend URL
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.FRONTEND_URL,
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        *[o.strip().rstrip("/") for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(videos.router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(clips.router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(publish.router, prefix="/api", dependencies=[Depends(require_admin)])
app.include_router(auth.router, prefix="/api")


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health_check():
    """Health check endpoint for monitoring."""
    return HealthResponse()


@app.get("/api/system-info", tags=["system"], dependencies=[Depends(require_admin)])
async def system_info():
    """
    The compute devices the worker uses (GPU or CPU, which encoder).

    The API and the worker run on different machines, so this is what the worker
    reported when it started; 503 until a worker has reported at least once.
    """
    from app.services import worker_info

    info = await worker_info.read()
    if info is None:
        raise HTTPException(status_code=503, detail="The worker has not reported yet.")
    return info
