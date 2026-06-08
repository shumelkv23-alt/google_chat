import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.interactions import router as interactions_router
from app.api.pubsub import router as pubsub_router
from app.config import settings
from app.logger import logger, setup_logging
from app.services.batch_processor import batch_ticker
from app.services.subscription import is_subscription_configured, renewal_loop

engine = create_async_engine(settings.database_url)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("app_started", base_url=settings.app_base_url, skip_jwt=settings.skip_jwt_validation)

    renewal_task: asyncio.Task | None = None
    if is_subscription_configured():
        renewal_task = asyncio.create_task(renewal_loop())
    else:
        logger.info("subscription_renewal_disabled")

    batch_task: asyncio.Task | None = None
    if settings.processing_mode == "batch":
        batch_task = asyncio.create_task(batch_ticker())
        logger.info(
            "batch_mode_enabled",
            size=settings.batch_size,
            timeout_s=settings.batch_timeout_seconds,
        )

    yield

    for task in (renewal_task, batch_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    await engine.dispose()
    logger.info("app_stopped")


app = FastAPI(lifespan=lifespan)
app.include_router(pubsub_router)
app.include_router(interactions_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
