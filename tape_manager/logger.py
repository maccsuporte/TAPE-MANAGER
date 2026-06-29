"""Setup de logging.

- Logger principal (arquivo rotativo + console)
- Logger dedicado para operações MTX (formato estruturado)
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config


_LOGGER_NAME = "tape_manager"
_MTX_LOGGER_NAME = "tape_manager.mtx"

_ALREADY_CONFIGURED = False


def setup_logging(cfg: Config) -> tuple[logging.Logger, logging.Logger]:
    """Idempotente: reconfigurar é seguro."""
    global _ALREADY_CONFIGURED
    main = logging.getLogger(_LOGGER_NAME)
    mtx = logging.getLogger(_MTX_LOGGER_NAME)

    # Limpa handlers antigos
    for lg in (main, mtx):
        for h in list(lg.handlers):
            lg.removeHandler(h)

    level = getattr(logging, cfg.logging.level.upper(), logging.INFO)
    main.setLevel(level)
    mtx.setLevel(logging.INFO)  # MTX sempre INFO para auditoria

    fmt_main = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt_mtx = logging.Formatter(
        "%(asctime)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    if cfg.logging.console:
        ch = logging.StreamHandler(stream=sys.stdout)
        ch.setFormatter(fmt_main)
        main.addHandler(ch)

    # Arquivo principal
    if cfg.logging.file:
        path = Path(cfg.logging.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            path,
            maxBytes=cfg.logging.max_size_mb * 1024 * 1024,
            backupCount=cfg.logging.backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt_main)
        main.addHandler(fh)

    # Arquivo MTX
    mtx_path = Path(cfg.logging.mtx_operations_log)
    mtx_path.parent.mkdir(parents=True, exist_ok=True)
    mfh = RotatingFileHandler(
        mtx_path,
        maxBytes=cfg.logging.max_size_mb * 1024 * 1024,
        backupCount=cfg.logging.backup_count,
        encoding="utf-8",
    )
    mfh.setFormatter(fmt_mtx)
    mtx.addHandler(mfh)

    _ALREADY_CONFIGURED = True
    return main, mtx


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def get_mtx_logger() -> logging.Logger:
    return logging.getLogger(_MTX_LOGGER_NAME)
