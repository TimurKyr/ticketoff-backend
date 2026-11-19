"""Application logging (BE-18: the app had none).

Our loggers are named "app.<module>" (e.g. "app.notifications"). This gives them one
stderr handler at INFO. They still propagate to the root logger, which uvicorn leaves
unconfigured, so nothing is printed twice, and pytest's caplog still sees every record.
"""

import logging

APP_LOGGER = "app"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent: safe to call from main.py and from each script."""
    logger = logging.getLogger(APP_LOGGER)
    logger.setLevel(level)
    if not any(getattr(handler, "_ticketoff", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler._ticketoff = True  # marks our handler, so a second call adds nothing
        logger.addHandler(handler)
