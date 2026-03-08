"""Structured logging setup with rotating file handler."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(config: dict = None) -> logging.Logger:
    """Set up logging with console + rotating file handler."""
    cfg = config or {}
    level = getattr(logging, cfg.get("level", "INFO"))
    log_file = cfg.get("file", "output/logs/money_mani.log")
    max_bytes = cfg.get("max_size_mb", 50) * 1024 * 1024
    backup_count = cfg.get("backup_count", 5)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("money_mani")
    if logger.handlers:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
