from contextlib import asynccontextmanager, closing

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .api import (
    ai_chat_router,
    anki_router,
    cards_router,
    collect_router,
    export_router,
    health_router,
    imports_router,
    media_router,
    modules_router,
    review_router,
    settings_router,
    stats_router,
)
from .api.settings import load_settings
from .aliases import backfill_history
from .collect import ensure_default_feed
from .db import connect, init_db
from .scheduler import scheduler


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with closing(connect()) as conn:
        backfill_history(conn)
    ensure_default_feed(load_settings())
    if config.SCHEDULER_ENABLED:
        scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="cardmine", version=config.APP_VERSION, lifespan=lifespan)
app.include_router(ai_chat_router)
app.include_router(anki_router)
app.include_router(cards_router)
app.include_router(collect_router)
app.include_router(export_router)
app.include_router(health_router)
app.include_router(imports_router)
app.include_router(media_router)
app.include_router(modules_router)
app.include_router(review_router)
app.include_router(settings_router)
app.include_router(stats_router)
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
app.mount(
    "/media",
    StaticFiles(directory=config.MEDIA_DIR, check_dir=False),
    name="media",
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "dashboard.html")


@app.get("/export", include_in_schema=False)
def export_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "export.html")


@app.get("/cards", include_in_schema=False)
def cards_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "cards.html")


@app.get("/cards/{card_id}", include_in_schema=False)
def card_page(card_id: int) -> FileResponse:
    return FileResponse(config.WEB_DIR / "card.html")


@app.get("/settings", include_in_schema=False)
def settings_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/import", include_in_schema=False)
def import_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "import.html")


@app.get("/posts", include_in_schema=False)
def posts_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "posts.html")


@app.get("/posts/{post_id}", include_in_schema=False)
def post_page(post_id: int) -> FileResponse:
    return FileResponse(config.WEB_DIR / "post.html")


@app.get("/review", include_in_schema=False)
def review_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "review.html")
