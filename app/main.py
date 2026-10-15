import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_sessionmaker
from app.worker.pipeline import Deps
from app.worker.scheduler import Worker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        init_db(engine)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        worker = Worker(Deps(settings=settings, factory=app.state.sessionmaker))
        app.state.worker = worker
        if settings.scheduler_enabled:
            worker.start()
        yield
        worker.shutdown()
        engine.dispose()

    app = FastAPI(title="Metacritic Games Tracker", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app


app = create_app()
