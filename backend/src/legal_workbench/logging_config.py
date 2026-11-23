"""Prevent configured credentials and PostgreSQL URLs from appearing in logs."""
import logging
import re
from urllib.parse import unquote, urlsplit
from .config import load_settings


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        settings = load_settings()
        message = logging.Formatter().format(record)
        password = urlsplit(settings.database_url).password if settings.database_url else None
        decoded_password = unquote(password) if password else None
        for secret in (settings.database_url, password, decoded_password):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"postgres(?:ql)?(?:\+\w+)?://[^\s]+", "[REDACTED DATABASE URL]", message)
        record.msg, record.args, record.exc_info, record.exc_text, record.stack_info = message, (), None, None, None
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactSecrets())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
