import atexit
import logging
import os
import queue
import sys
import time
import unicodedata
import functools
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

    ACTION_MAP = {
        "FILL_GRID": "补单检查", "ORDER_SENT": "下单成功", "RM_FAR": "清理远单",
        "WINDOW_LIMIT": "窗口限制", "CONFIG_ERROR": "配置错误", "WARN": "系统警告",
        "ERROR": "运行错误", "CRITICAL": "严重错误", "SLEEP": "暂停运行",
        "CLEANUP": "清理旧单", "SYSTEM": "系统消息", "RELOAD": "重载配置",
        "ADD": "新增策略", "UPDATE": "更新策略", "REMOVE": "移除策略",
        "START": "系统启动", "ORDER_FAIL": "下单失败", "ORDER_CHECK": "挂单检查",
        "EXCEPTION": "未知异常", "TRIM": "修剪挂单", "STATUS": "状态巡检",
        "ACCOUNT": "资金播报", "SKIP": "跳过补单", "DEBUG": "调试信息",
        "STOP": "策略停止", "HALT": "熔断暂停", "SHUTDOWN": "系统关闭",
        "STATS_RESET": "统计重置", "INIT": "初始化", "RECENTER": "锚点平移",
        "FUSE": "点差熔断", "HEDGE_ADD": "对冲加仓", "HEDGE_EXIT": "对冲平仓",
    }

    ACTION_LEVEL_MAP = {
        "DEBUG": "debug", "WARN": "warning", "HALT": "warning", "SLEEP": "warning",
        "FUSE": "warning", "ERROR": "error", "EXCEPTION": "error",
        "ORDER_FAIL": "error", "CONFIG_ERROR": "error", "CRITICAL": "critical",
    }

    NOISY_ACTIONS = {
        "FILL_GRID", "RM_FAR", "WINDOW_OPT", "WARN", "ERROR",
        "ORDER_FAIL", "EXCEPTION", "HALT", "SKIP", "DEBUG",
    }

    _LEVEL_STR_MAP = {
        "debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING,
        "warning": logging.WARNING, "error": logging.ERROR, "critical": logging.CRITICAL,
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
    @functools.lru_cache(maxsize=128)
    def _display_width(text: str) -> int:
        width = 0
        for ch in text:
            if unicodedata.east_asian_width(ch) in {"F", "W"}:
                width += 2
            else:
                width += 1
        return width

    @classmethod
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
                cls._enable_console = os.getenv("INV_LOG_CONSOLE", "1").strip().lower() in {"1", "true", "yes", "y", "on"}
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
                    console_format = _MessageFormatter("console_msg", "%(asctime)s.%(msecs)03d | %(message)s", datefmt="%H:%M:%S")
                    console_handler.setFormatter(console_format)
                    handlers.append(console_handler)

                LOG_DIR.mkdir(parents=True, exist_ok=True)

                file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
                file_format = _MessageFormatter("file_msg", "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
                file_handler.setFormatter(file_format)
                handlers.append(file_handler)

                cls._queue = queue.Queue(-1)
                queue_handler = QueueHandler(cls._queue)
                cls._logger.addHandler(queue_handler)

                cls._listener = QueueListener(cls._queue, *handlers, respect_handler_level=True)
                cls._listener.start()
                atexit.register(cls._stop_listener)

            if cls._aggregate_interval is None:
                try: cls._aggregate_interval = float(os.getenv("INV_LOG_AGG_SECONDS", "10"))
                except Exception: cls._aggregate_interval = 0.0

            if cls._throttle_seconds is None:
                try: cls._throttle_seconds = float(os.getenv("INV_LOG_THROTTLE_SECONDS", "1.5"))
                except Exception: cls._throttle_seconds = 1.5

    @classmethod
    def _stop_listener(cls):
        if cls._listener:
            cls._listener.stop()
            cls._listener = None

    @classmethod
    def log(cls, symbol, action, message, level="info"):
        cls._ensure_logger()

        # 1. 统一 level 处理（复用变量，避免重复转换）
        level_lower = str(level).lower() if level is not None else "info"
        if level_lower == "info" and action in cls.ACTION_LEVEL_MAP:
            level_lower = cls.ACTION_LEVEL_MAP[action]

        # 2. 一次性转换并复用
        str_symbol = str(symbol)
        action_cn = cls.ACTION_MAP.get(action, action)
        str_action_cn = str(action_cn)

        # 3. 动态调整列宽（利用缓存）
        symbol_width = cls._display_width(str_symbol)
        if symbol_width > cls._symbol_width:
            cls._symbol_width = symbol_width
            
        action_width = cls._display_width(str_action_cn)
        if action_width > cls._action_width:
            cls._action_width = action_width

        # 4. 聚合逻辑（增加过期清理）
        if cls._aggregate_interval and action in cls._aggregate_actions:
            now = time.monotonic()
            key = (str_symbol, action)
            entry = cls._aggregate_buffer.get(key)
            if entry is None:
                cls._aggregate_buffer[key] = {"count": 0, "last_message": message, "last_emit": now}
            else:
                entry["count"] += 1
                entry["last_message"] = message
                if (now - entry["last_emit"]) < cls._aggregate_interval:
                    return
                if entry["count"] > 0:
                    message = f"{entry['count']}x | last: {entry['last_message']}"
                    entry["count"] = 0
                entry["last_emit"] = now

            # ✨ 关键修改点 1：聚合缓冲区定期清理（每10次聚合检查触发一次，避免无限增长）
            if len(cls._aggregate_buffer) > 500:
                cutoff = now - cls._aggregate_interval * 2
                # 使用迭代删除避免创建新字典
                keys_to_del = [k for k, v in cls._aggregate_buffer.items() if v["last_emit"] < cutoff]
                for k in keys_to_del:
                    del cls._aggregate_buffer[k]

        # 5. 限流逻辑（优化清理策略）
        throttle_sec = cls._throttle_seconds  # 已确保有值
        if throttle_sec > 0 and action in cls.NOISY_ACTIONS:
            now = time.monotonic()
            key = (str_symbol, action, str(message), level_lower)  # message 已为字符串
            last = cls._last_emit_ts.get(key)
            if last is not None and (now - last) < throttle_sec:
                return
            cls._last_emit_ts[key] = now

            # ✨ 关键修改点 2：限流字典清理改为惰性删除 + 按时间戳过期清理
            # 仅在字典过大且插入新 key 时触发，避免频繁全量遍历
            if len(cls._last_emit_ts) > 2000:
                cutoff = now - throttle_sec * 2
                # 使用迭代删除过期条目，避免 O(N) 创建新字典
                expired_keys = [k for k, ts in cls._last_emit_ts.items() if ts < cutoff]
                for k in expired_keys:
                    del cls._last_emit_ts[k]
                # 如果仍然过大，强制缩小到 1500（按最近时间保留）
                if len(cls._last_emit_ts) > 1800:
                    # 按时间戳排序，保留最新的 1500 个
                    sorted_items = sorted(cls._last_emit_ts.items(), key=lambda x: x[1], reverse=True)[:1500]
                    cls._last_emit_ts = dict(sorted_items)

        # 6. 构造日志消息（复用已计算的宽度）
        symbol_pad = cls._pad_display(str_symbol, cls._symbol_width, current_width=symbol_width)
        action_pad = cls._pad_display(str_action_cn, cls._action_width, current_width=action_width)
        
        file_msg = f"{symbol_pad} | [{action_pad}] | {message}"

        # 7. 控制台颜色（复用 level_lower，避免重复 upper）
        color = cls._ACTION_COLOR_MAP.get(action, Colors.RESET)
        if color == Colors.RESET:
            if level_lower in ("error", "critical"):
                color = Colors.RED
            elif level_lower in ("warn", "warning"):
                color = Colors.YELLOW

        console_msg = f"{color}{symbol_pad} | [{action_pad}] | {message}{Colors.RESET}"

        # 8. 创建日志记录并提交
        levelno = cls._LEVEL_STR_MAP.get(level_lower, logging.INFO)
        record = logging.LogRecord("GridTrading", levelno, "", 0, file_msg, (), None)
        record.file_msg = file_msg
        record.console_msg = console_msg
        record.created = time.time()
        cls._logger.handle(record)