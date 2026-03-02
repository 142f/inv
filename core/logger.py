import atexit
import logging
import os
import queue
import sys
import time
import unicodedata
import functools  # ✨ 新增：用于引入 LRU 缓存
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
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
    _symbol_width = 12
    _action_width = 8
    _listener = None
    _queue = None
    _aggregate_actions = {"SKIP"}
    _aggregate_interval = None
    _aggregate_buffer = {}
    _throttle_seconds = None

    # 映射表提升为类常量
    ACTION_MAP = {
        "FILL_GRID": "补单检查",
        "ORDER_SENT": "下单成功",
        "RM_FAR": "清理远单",
        "WINDOW_LIMIT": "窗口限制",
        "CONFIG_ERROR": "配置错误",
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
        "ORDER_CHECK": "挂单检查",
        "EXCEPTION": "未知异常",
        "TRIM": "修剪挂单",
        "STATUS": "状态巡检",
        "ACCOUNT": "资金播报",
        "SKIP": "跳过补单",
        "DEBUG": "调试信息",
        "STOP": "策略停止",
        "HALT": "熔断暂停",
        "SHUTDOWN": "系统关闭",
        "STATS_RESET": "统计重置",
        "INIT": "初始化",
        "RECENTER": "锚点平移",
        "FUSE": "点差熔断",
        "HEDGE_ADD": "对冲加仓",
        "HEDGE_EXIT": "对冲平仓",
    }

    ACTION_LEVEL_MAP = {
        "DEBUG": "debug",
        "WARN": "warning",
        "HALT": "warning",
        "SLEEP": "warning",
        "FUSE": "warning",
        "ERROR": "error",
        "EXCEPTION": "error",
        "ORDER_FAIL": "error",
        "CONFIG_ERROR": "error",
        "CRITICAL": "critical",
    }

    NOISY_ACTIONS = {
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

    _LEVEL_STR_MAP = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }

    _ACTION_COLOR_MAP = {
        "ERROR": Colors.RED, "EXCEPTION": Colors.RED, "CRITICAL": Colors.RED, "ORDER_FAIL": Colors.RED,
        "WARN": Colors.YELLOW, "HALT": Colors.YELLOW, "SLEEP": Colors.YELLOW,
        "ORDER_SENT": Colors.GREEN, "ADD": Colors.GREEN, "START": Colors.GREEN, "RELOAD": Colors.GREEN,
        "STATUS": Colors.CYAN, "ACCOUNT": Colors.CYAN,
        "TRIM": Colors.MAGENTA, "CLEANUP": Colors.MAGENTA, "REMOVE": Colors.MAGENTA,
        "SKIP": Colors.GREY, "DEBUG": Colors.GREY,
    }

    @staticmethod
    @functools.lru_cache(maxsize=128)  # ✨ 关键修改点 1：增加缓存，高频复用的交易对和Action名可实现 O(1) 提取
    def _display_width(text: str) -> int:
        width = 0
        for ch in text:
            if unicodedata.east_asian_width(ch) in {"F", "W"}:
                width += 2
            else:
                width += 1
        return width

    @classmethod
    # ✨ 关键修改点 2：新增 current_width 传参，若外部已计算过宽度则直接利用，消除隐式二次计算
    def _pad_display(cls, text: str, target_width: int, current_width: int = None) -> str:
        text = str(text)
        actual_width = current_width if current_width is not None else cls._display_width(text)
        pad = target_width - actual_width
        if pad <= 0:
            return text
        return text + (" " * pad)


    @classmethod
    def _ensure_logger(cls):
        if cls._logger is None:
            cls._logger = logging.getLogger("GridTrading")
            cls._logger.setLevel(logging.INFO)
            cls._logger.propagate = False

            if not cls._logger.handlers:
                cls._enable_console = os.getenv("INV_LOG_CONSOLE", "1").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "y",
                    "on",
                }

                handlers = []

                class _MessageFormatter(logging.Formatter):
                    def __init__(self, message_attr, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        self.message_attr = message_attr

                    def format(self, record):
                        if hasattr(record, self.message_attr):
                            record.msg = getattr(record, self.message_attr)
                            record.args = ()
                        return super().format(record)

                if cls._enable_console:
                    console_handler = logging.StreamHandler(sys.stdout)
                    console_format = _MessageFormatter(
                        "console_msg", "%(asctime)s.%(msecs)03d | %(message)s", datefmt="%H:%M:%S"
                    )
                    console_handler.setFormatter(console_format)
                    handlers.append(console_handler)

                LOG_DIR.mkdir(parents=True, exist_ok=True)

                file_handler = RotatingFileHandler(
                    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
                )
                file_format = _MessageFormatter(
                    "file_msg", "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
                )
                file_handler.setFormatter(file_format)
                handlers.append(file_handler)

                cls._queue = queue.Queue(-1)
                queue_handler = QueueHandler(cls._queue)
                cls._logger.addHandler(queue_handler)

                cls._listener = QueueListener(cls._queue, *handlers, respect_handler_level=True)
                cls._listener.start()
                atexit.register(cls._stop_listener)

            if cls._aggregate_interval is None:
                try:
                    cls._aggregate_interval = float(os.getenv("INV_LOG_AGG_SECONDS", "10"))
                except Exception:
                    cls._aggregate_interval = 0.0

            if cls._throttle_seconds is None:
                try:
                    cls._throttle_seconds = float(os.getenv("INV_LOG_THROTTLE_SECONDS", "1.5"))
                except Exception:
                    cls._throttle_seconds = 1.5

    @classmethod
    def _stop_listener(cls):
        if cls._listener:
            cls._listener.stop()
            cls._listener = None

    @classmethod
    def log(cls, symbol, action, message, level="info"):
        cls._ensure_logger()

        level_str = str(level) if level is not None else "info"
        if level_str.lower() == "info" and action in cls.ACTION_LEVEL_MAP:
            level_str = cls.ACTION_LEVEL_MAP[action]
        level = level_str

        action_cn = cls.ACTION_MAP.get(action, action)

        # ✨ 关键修改点 3：提前统一转为字符串类型，避免底层重复调用 str() 解析
        str_symbol = str(symbol)
        str_action_cn = str(action_cn)

        symbol_width = cls._display_width(str_symbol)
        if symbol_width > cls._symbol_width:
            cls._symbol_width = symbol_width
            
        action_width = cls._display_width(str_action_cn)
        if action_width > cls._action_width:
            cls._action_width = action_width

        throttle_seconds = cls._throttle_seconds if cls._throttle_seconds is not None else 1.5

        if cls._aggregate_interval and action in cls._aggregate_actions:
            now = time.monotonic()
            key = (str_symbol, str(action))
            entry = cls._aggregate_buffer.get(key)
            if entry is None:
                cls._aggregate_buffer[key] = {
                    "count": 0,
                    "last_message": message,
                    "last_emit": now,
                }
            else:
                entry["count"] += 1
                entry["last_message"] = message
                if (now - entry["last_emit"]) < cls._aggregate_interval:
                    return
                if entry["count"] > 0:
                    message = f"{entry['count']}x | last: {entry['last_message']}"
                    entry["count"] = 0
                entry["last_emit"] = now

        if throttle_seconds > 0 and action in cls.NOISY_ACTIONS:
            now = time.monotonic()
            # ✨ 关键修改点 4：复用已被初始化的小写 level，避免重复调用 str().lower()
            key = (str_symbol, str(action), str(message), level.lower())
            last = cls._last_emit_ts.get(key)
            if last is not None and (now - last) < throttle_seconds:
                return
            cls._last_emit_ts[key] = now
            # Prevent unbounded growth of the throttle cache
            if len(cls._last_emit_ts) > 2000:
                cutoff = now - throttle_seconds * 2
                cls._last_emit_ts = {k: v for k, v in cls._last_emit_ts.items() if v > cutoff}

        # ✨ 关键修改点 5：显式传入外层已计算出的字符宽度，彻底截断内部的二次计算
        symbol_pad = cls._pad_display(str_symbol, cls._symbol_width, current_width=symbol_width)
        action_pad = cls._pad_display(str_action_cn, cls._action_width, current_width=action_width)
        
        file_msg = f"{symbol_pad} | [{action_pad}] | {message}"

        level_upper = level.upper()
        color = cls._ACTION_COLOR_MAP.get(action, Colors.RESET)
        if color == Colors.RESET and level_upper in ("ERROR", "CRITICAL"):
            color = Colors.RED
        elif color == Colors.RESET and level_upper in ("WARN", "WARNING"):
            color = Colors.YELLOW

        console_msg = f"{color}{symbol_pad} | [{action_pad}] | {message}{Colors.RESET}"

        levelno = cls._LEVEL_STR_MAP.get(level.lower(), logging.INFO)
        record = logging.LogRecord("GridTrading", levelno, "", 0, file_msg, (), None)
        record.file_msg = file_msg
        record.console_msg = console_msg
        record.created = time.time()
        cls._logger.handle(record)