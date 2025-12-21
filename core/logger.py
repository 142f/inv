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
        
        # 汉化动作映射 (统一为 4 个汉字以保证对齐)
        action_map = {
            "FILL_GRID": "补单检查",
            "ORDER_SENT": "下单成功",
            "RM_FAR": "清理远单",
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
            "EXCEPTION": "未知异常"
        }
        
        action_cn = action_map.get(action, action)
        
        # 优化对齐格式：
        # Symbol: 9字符 (BTCUSDc)
        # Action: 4个汉字 (不再使用 <8 填充，因为汉字宽度在不同终端不一致，定长最安全)
        log_func(f"{symbol:<9} | {action_cn} | {message}")
