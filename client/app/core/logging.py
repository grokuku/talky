# -*- coding: utf-8 -*-
"""
app/core/logging.py
===================
Configuration centralisée du logging de l'application.
"""

import logging
from typing import Optional

_LOGGER_NAME = "talky"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure le logging racine et retourne le logger de l'application."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return get_logger()


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)
