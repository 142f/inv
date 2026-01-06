import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class Colors:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GREY = "\033[90m"


# Project root is the repo root (current directory containing core/)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "grid_trading.log"


class Logger:
    _logger = None
    _last_emit_ts = {}
    _enable_console = False

    @classmethod
    def _ensure_logger(cls):
        if cls._logger is None:
            cls._logger = logging.getLogger("GridTrading")
            cls._logger.setLevel(logging.INFO)

            if not cls._logger.handlers:
                cls._enable_console = os.getenv("INV_LOG_CONSOLE", "1").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "y",
                    "on",
                }

                if cls._enable_console:
                    console_handler = logging.StreamHandler(sys.stdout)
                    console_format = logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S")
                    console_handler.setFormatter(console_format)
                    cls._logger.addHandler(console_handler)

                LOG_DIR.mkdir(parents=True, exist_ok=True)

                file_handler = RotatingFileHandler(
                    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
                )
                file_format = logging.Formatter(
                    "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
                )
                file_handler.setFormatter(file_format)
                cls._logger.addHandler(file_handler)

    @staticmethod
    def log(symbol, action, message, level="info"):
        Logger._ensure_logger()

        action_map = {
            "FILL_GRID": "补单检查",
            "ORDER_SENT": "下单成功",
            "RM_FAR": "清理远单",
            "WINDOW_LIMIT": "窗口限制",
            "WARN": "系统警告",
            "ERROR": "运行错误",
            "CRITICAL": "严重错误",
            "SLEEP": "暂停运行",
            "CLEANUP": "清理旧单",
            "SYSTEM": "系统消息",
            "RELOAD": "重载配置",
            "ADD": "新增策略",
            "UPDATE": "更新策略",
            "REMOVE": "移除策略",
            "START": "系统启动",
            "ORDER_FAIL": "下单失败",
            "EXCEPTION": "未知异常",
            "TRIM": "修剪挂单",
            "STATUS": "状态巡检",
            "ACCOUNT": "资金播报",
            "SKIP": "跳过补单",
            "DEBUG": "调试信息",
            "STOP": "策略停止",
            "HALT": "熔断暂停",
            "SHUTDOWN": "系统关闭",
        }

        action_cn = action_map.get(action, action)

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
            "SKIP",
            "DEBUG",
        }

        if throttle_seconds > 0 and action in noisy_actions:
            now = time.monotonic()
            key = (str(symbol), str(action), str(message), str(level).lower())
            last = Logger._last_emit_ts.get(key)
            if last is not None and (now - last) < throttle_seconds:
                return
            Logger._last_emit_ts[key] = now

        file_msg = f"{symbol:<10} | 【{action_cn}】 | {message}"

        if Logger._enable_console:
            color = Colors.RESET
            if level.upper() == "ERROR" or action in {"ERROR", "EXCEPTION", "CRITICAL", "ORDER_FAIL"}:
                color = Colors.RED
            elif level.upper() == "WARN" or action in {"WARN", "HALT", "SLEEP"}:
                color = Colors.YELLOW
            elif action in {"ORDER_SENT", "ADD", "START", "RELOAD"}:
                color = Colors.GREEN
            elif action in {"STATUS", "ACCOUNT"}:
                color = Colors.CYAN
            elif action in {"TRIM", "CLEANUP", "REMOVE"}:
                color = Colors.MAGENTA
            elif action in {"SKIP", "DEBUG"}:
                color = Colors.GREY

            console_msg = f"{color}{symbol:<10} | 【{action_cn}】 | {message}{Colors.RESET}"

            for handler in Logger._logger.handlers:
                if isinstance(handler, RotatingFileHandler):
                    record = logging.LogRecord("GridTrading", logging.INFO, "", 0, file_msg, (), None)
                    record.created = time.time()
                    handler.emit(record)
                elif isinstance(handler, logging.StreamHandler):
                    record = logging.LogRecord("GridTrading", logging.INFO, "", 0, console_msg, (), None)
                    record.created = time.time()
                    handler.emit(record)
        else:
            Logger._logger.info(file_msg)
