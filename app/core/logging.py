import logging

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Configure application logging once, at startup.

    Uses the standard library so log records integrate with uvicorn/Gunicorn
    handlers in production. Security-relevant events (login success/failure,
    logout, registration) are logged by the auth layer via module loggers.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _CONFIGURED = True
