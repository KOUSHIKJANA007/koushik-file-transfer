"""Logging configuration for WebRTC MVP."""
import logging
from .config import config


def setup_logger(name: str) -> logging.Logger:
    """Setup and configure logger."""
    logger = logging.getLogger(name)
    
    # Avoid duplicate handlers
    if logger.handlers:
        return logger
    
    logger.setLevel(getattr(logging, config.LOG_LEVEL))
    
    handler = logging.StreamHandler()
    formatter = logging.Formatter(config.LOG_FORMAT)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger
