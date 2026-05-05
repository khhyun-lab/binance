from __future__ import annotations

import logging
import os
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler


LOG_SCHEMA_VERSION = "2026-04-30-3m-v2"


def _resolve_log_backup_count() -> int:
    retention_days = 2
    raw_value = os.getenv("LOG_RETENTION_DAYS")
    if raw_value and raw_value.strip():
        retention_days = max(1, int(raw_value))
    return max(0, retention_days - 1)


def setup_logging(log_dir: Path, log_level: str, log_filename: str = "bot.log") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / log_filename

    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=_resolve_log_backup_count(),
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler()

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            file_handler,
            console_handler,
        ],
        force=True,
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("upbit._base_client").setLevel(logging.WARNING)
