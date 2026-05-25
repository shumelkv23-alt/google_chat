from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.interactions import router as interactions_router
from app.api.pubsub import router as pubsub_router
from app.config import settings
from app.logger import logger, setup_logging

engine = create_async_engine(settings.database_url)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("app_started", base_url=settings.app_base_url, skip_jwt=settings.skip_jwt_validation)
    yield
    await engine.dispose()
    logger.info("app_stopped")


app = FastAPI(lifespan=lifespan)
app.include_router(pubsub_router)
app.include_router(interactions_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
