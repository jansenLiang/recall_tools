from __future__ import annotations

import logging
import sys
from pathlib import Path

from app.core.config import settings


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    log_path = Path(settings.log_file)
    if not log_path.is_absolute():
        log_path = Path.cwd() / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    logging.getLogger(__name__).info("persistent logging enabled file=%s", log_path)
    logging.getLogger("httpx").setLevel(logging.WARNING)
