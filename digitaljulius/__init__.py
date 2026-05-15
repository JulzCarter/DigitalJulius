__version__ = "0.2.0"

# Initialise the central logger as soon as the package is imported. Anything
# else that calls `get_logger(__name__)` later just inherits the config.
from digitaljulius.log import setup as _setup_logging
try:
    _setup_logging()
except Exception:
    # Never let logging setup kill the import — fall through silently.
    pass
