# logger.py
import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime
import time

class Logger:
    _logger = None
    _last_emit_ts = {}

    @classmethod
    def _ensure_logger(cls):
        if cls._logger is None:
            cls._logger = logging.getLogger("GridTrading")
            cls._logger.setLevel(logging.INFO)
            
            # Prevent adding handlers multiple times
            if not cls._logger.handlers:
                # Console output (disabled by default to avoid continuous printing)
                enable_console = os.getenv("INV_LOG_CONSOLE", "0").strip().lower() in {"1", "true", "yes", "y", "on"}
                if enable_console:
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

                if enable_console:
                    cls._logger.addHandler(console_handler)
                cls._logger.addHandler(file_handler)

    @staticmethod
    def log(symbol, action, message, level="info"):
        Logger._ensure_logger()
        log_func = getattr(Logger._logger, level.lower(), Logger._logger.info)
        
        # 汉化动作映射 (统一为 4 个汉字以保证对齐)
        action_map = {
            "FILL_GRID":    "补单检查",
            "ORDER_SENT":   "下单成功",
            "RM_FAR":       "清理远单",
            "WINDOW_LIMIT": "窗口限制",
            "WARN":         "系统警告",
            "ERROR":        "运行错误",
            "CRITICAL":     "严重错误",
            "SLEEP":        "暂停运行",
            "CLEANUP":      "清理旧单",
            "SYSTEM":       "系统消息",
            "RELOAD":       "重载配置",
            "ADD":          "新增策略",
            "UPDATE":       "更新策略",
            "REMOVE":       "移除策略",
            "START":        "系统启动",
            "ORDER_FAIL":   "下单失败",
            "EXCEPTION":    "未知异常"
        }
        
        action_cn = action_map.get(action, action)

        # 简单节流：相同的 (symbol, action, message, level) 在短时间内重复时丢弃
        # 目的：避免错误/重试情况下刷屏或持续写日志
        try:
            throttle_seconds = float(os.getenv("INV_LOG_THROTTLE_SECONDS", "1.5"))
        except Exception:
            throttle_seconds = 1.5

        noisy_actions = {
            "FILL_GRID",
            "RM_FAR",
            "WINDOW_OPT",
            "WARN",
            "ERROR",
            "ORDER_FAIL",
            "EXCEPTION",
            "HALT",
        }
        if throttle_seconds > 0 and action in noisy_actions:
            now = time.monotonic()
            key = (str(symbol), str(action), str(message), str(level).lower())
            last = Logger._last_emit_ts.get(key)
            if last is not None and (now - last) < throttle_seconds:
                return
            Logger._last_emit_ts[key] = now
        
        # 优化对齐格式：
        # Symbol: 10字符 (BTCUSDc   )
        # Action: 【xxxx】 增加区分度
        log_func(f"{symbol:<10} | 【{action_cn}】 | {message}")
