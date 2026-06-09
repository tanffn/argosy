"""Structlog setup. JSON-formatted logs to file + stderr.

Phase 0 keeps it simple: a single app log under
`${ARGOSY_HOME}/logs/app/application.log` (per SDD §14.1). Cadence-specific log streams
(per SDD §14.1) come in later phases.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

from argosy.config import get_settings

_CONFIGURED = False


def configure_logging(level: int | str = logging.INFO) -> None:
    """Idempotent. Wires stdlib `logging` to structlog with JSON renderer.

    ``cache_logger_on_first_use=False`` is intentional: caching structlog
    BoundLoggerLazyProxy instances across tests causes caplog to see empty
    records — the cached proxy binds to the processor chain and stdlib logger
    at first-call time, but pytest's LogCaptureHandler is added to the root
    logger AFTER collection-time imports. Disabling the cache means every
    log call rebuilds the chain fresh, so caplog can always intercept records
    regardless of test ordering. The performance cost is negligible (processor
    chain evaluation is I/O-bound in practice).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    log_file: Path = settings.app_log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Disabled: caching here causes pytest caplog isolation failures.
        # When a BoundLoggerLazyProxy is first-used in a test that runs
        # without a caplog handler active, the cached chain bypasses
        # caplog's handler for all later tests in the same session.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers if reconfigured.
    root.handlers = [file_handler, stderr_handler]

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.stdlib.get_logger(name)
