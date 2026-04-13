"""Logging configuration for the sync engine."""

import logging

_logger = None


def get_logger() -> logging.Logger:
    """Return the shared logger for the sync package."""
    global _logger
    if _logger is None:
        _logger = logging.getLogger("cortex_jira_sync")
        if not _logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            )
            _logger.addHandler(handler)
            _logger.setLevel(logging.INFO)
    return _logger
