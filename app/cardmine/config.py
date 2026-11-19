"""应用配置：全部来自环境变量，默认值适配本地开发与容器运行。"""

import os
from pathlib import Path

APP_VERSION = "0.1.0"

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR.parent / "web"

DATA_DIR = Path(os.environ.get("CARDMINE_DATA_DIR", BASE_DIR.parents[1] / "data"))
DB_PATH = DATA_DIR / "cardmine.db"
MEDIA_DIR = DATA_DIR / "media"

ANKI_CONNECT_URL = os.environ.get(
    "ANKI_CONNECT_URL", "http://anki-connect-server:8765/api"
)

# Anki 容器数据目录（只读挂载），用于同步前备份集合与媒体
ANKI_DATA_DIR = Path(os.environ.get("ANKI_DATA_DIR", DATA_DIR / "anki"))
BACKUP_DIR = DATA_DIR / "backups" / "anki"

# 进程内定时抓取，本地调试可用 CARDMINE_SCHEDULER=0 关闭
SCHEDULER_ENABLED = os.environ.get("CARDMINE_SCHEDULER", "1") != "0"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
