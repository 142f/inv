"""MT5 broker adapter."""

from __future__ import annotations

import os
import threading
from typing import Any

import MetaTrader5 as mt5
from dotenv import load_dotenv

from core.logger import Logger
from core.security import Security

from .base import BrokerBase


class MT5Broker(BrokerBase):
    def __init__(self) -> None:
        load_dotenv()
        self._lock = threading.Lock()
        self.security = Security()

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def _decrypt_env(self, value: str | None) -> str | None:
        if value and value.startswith("gAAAA"):
            decrypted = self.security.decrypt(value)
            return decrypted if decrypted else value
        return value

    def initialize(self) -> bool:
        acc_id_str = self._decrypt_env(os.getenv("MT5_ACCOUNT_ID") or "")
        pwd = self._decrypt_env(os.getenv("MT5_PASSWORD") or "")
        srv = self._decrypt_env(os.getenv("MT5_SERVER") or "")
        mt5_path = self._decrypt_env(os.getenv("MT5_PATH") or "")

        acc_id = int(acc_id_str) if acc_id_str and acc_id_str.isdigit() else 0

        init_params: dict[str, Any] = {}
        if mt5_path:
            init_params["path"] = mt5_path

        with self._lock:
            if not mt5.initialize(**init_params):
                if init_params and not mt5.initialize():
                    Logger.log("SYSTEM", "ERROR", f"MT5 Init Failed: {mt5.last_error()}")
                    return False
                if not init_params:
                    Logger.log("SYSTEM", "ERROR", f"MT5 Init Failed: {mt5.last_error()}")
                    return False

            current_account_info = mt5.account_info()
            if current_account_info:
                mode_str = "Unknown"
                if current_account_info.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
                    mode_str = "HEDGING (对冲模式)"
                elif current_account_info.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING:
                    mode_str = "NETTING (净额模式)"
                else:
                    mode_str = f"Mode {current_account_info.margin_mode}"
                Logger.log("SYSTEM", "INFO", f"账户模式: {mode_str}")
                if current_account_info.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING:
                    Logger.log(
                        "SYSTEM",
                        "WARN",
                        "注意: 当前策略非 HEDGING 设计，在 NETTING 模式下可能无法正确管理多层网格持仓。",
                    )

            if acc_id != 0 and current_account_info and current_account_info.login == acc_id:
                Logger.log(
                    "SYSTEM",
                    "INFO",
                    f"检测到终端已登录账号 {acc_id}，跳过重复登录",
                )
                return True

            if acc_id != 0:
                Logger.log("SYSTEM", "INFO", f"正在尝试登录账号 {acc_id}...")
                if not mt5.login(acc_id, password=pwd, server=srv):
                    Logger.log(
                        "SYSTEM",
                        "ERROR",
                        f"Login Failed: {mt5.last_error()} (请检查.env 中的账号/密码/服务器)",
                    )
                    return False
            else:
                if current_account_info:
                    Logger.log(
                        "SYSTEM",
                        "WARN",
                        f"未配置指定账号，使用当前终端账号: {current_account_info.login}",
                    )
                else:
                    Logger.log("SYSTEM", "ERROR", "未配置账号且当前终端未登录")
                    return False

        return True

    def shutdown(self) -> None:
        with self._lock:
            if mt5.terminal_info() is not None:
                mt5.shutdown()
                mt5.shutdown()
        Logger.log("SYSTEM", "SHUTDOWN", "MT5连接已关闭")

    def ensure_symbol(self, symbol: str) -> None:
        with self._lock:
            mt5.symbol_select(symbol, True)

    def account_info(self) -> Any:
        return mt5.account_info()

    def terminal_info(self) -> Any:
        return mt5.terminal_info()

    def orders_get(self) -> Any:
        return mt5.orders_get()

    def positions_get(self) -> Any:
        return mt5.positions_get()

    def symbol_info_tick(self, symbol: str) -> Any:
        return mt5.symbol_info_tick(symbol)

    def order_send(self, request: dict) -> Any:
        return mt5.order_send(request)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Any:
        return mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)
