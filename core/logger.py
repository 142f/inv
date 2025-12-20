# logger.py
import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime

class Logger:
    _logger = None

    @classmethod
    def _ensure_logger(cls):
        if cls._logger is None:
            cls._logger = logging.getLogger("GridTrading")
            cls._logger.setLevel(logging.INFO)
            
            # Prevent adding handlers multiple times
            if not cls._logger.handlers:
                # Console output
                console_handler = logging.StreamHandler()
                console_format = logging.Formatter(
                    "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"
                )
                console_handler.setFormatter(console_format)
                
                # File output (Rotating)
                # Ensure log directory exists
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                log_dir = os.path.join(base_dir, "logs")
                if not os.path.exists(log_dir):
                    os.makedirs(log_dir)
                
                log_file = os.path.join(log_dir, "grid_trading.log")
                file_handler = RotatingFileHandler(
                    log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
                )
                file_format = logging.Formatter(
                    "%(asctime)s | %(levelname)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"
                )
                file_handler.setFormatter(file_format)
                
                cls._logger.addHandler(console_handler)
                cls._logger.addHandler(file_handler)

    @staticmethod
    def log(symbol, action, message, level="info"):
        Logger._ensure_logger()
        log_func = getattr(Logger._logger, level.lower(), Logger._logger.info)
        log_func(f"{symbol:<9} | {action:<10} | {message}")
