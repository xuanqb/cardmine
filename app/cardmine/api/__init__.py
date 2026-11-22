from .ai_chat import router as ai_chat_router
from .anki import router as anki_router
from .cards import router as cards_router
from .collect import router as collect_router
from .export import router as export_router
from .health import router as health_router
from .imports import router as imports_router
from .media import router as media_router
from .modules import router as modules_router
from .review import router as review_router
from .settings import router as settings_router
from .stats import router as stats_router

__all__ = [
    "ai_chat_router",
    "anki_router",
    "cards_router",
    "collect_router",
    "export_router",
    "health_router",
    "imports_router",
    "media_router",
    "modules_router",
    "review_router",
    "settings_router",
    "stats_router",
]
