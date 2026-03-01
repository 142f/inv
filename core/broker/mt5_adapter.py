"""MT5 broker adapter."""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
import threading
from typing import Any

import MetaTrader5 as mt5
from dotenv import load_dotenv

from core.logger import Logger
from core.security import Security


class BrokerBase(ABC):
    """Abstract base class for broker adapters."""

    @property
    @abstractmethod
    def lock(self) -> Any:
        """Shared lock for broker interactions (RLock recommended)."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if connected to the broker/terminal."""

    @abstractmethod
    def initialize(self) -> bool:
        """
        Initialize broker connection.
        Returns:
            bool: True if successful, False otherwise.
        """

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown broker connection and clean up resources."""

    @abstractmethod
    def ensure_symbol(self, symbol: str) -> bool:
        """
        Ensure a symbol is available and selected for data feed.
        """

    @abstractmethod
    def account_info(self) -> Any:
        """Return account info from broker."""

    @abstractmethod
    def terminal_info(self) -> Any:
        """Return terminal info from broker."""

    @abstractmethod
    def orders_get(self) -> Any:
        """Return active orders."""

    @abstractmethod
    def positions_get(self) -> Any:
        """Return active positions."""

    @abstractmethod
    def symbol_info_tick(self, symbol: str) -> Any:
        """Return latest tick for symbol."""

    @abstractmethod
    def order_send(self, request: dict) -> Any:
        """Send a trade request."""

    @abstractmethod
    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Any:
        """Return rates from position."""


class MT5Broker(BrokerBase):
    def __init__(self) -> None:
        load_dotenv()
        self._lock = threading.RLock()
        self.security = Security()
        self._subscribed_symbols: set[str] = set()
        self._connected = False

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _decrypt_env(self, env_name: str, value: str | None) -> str:
        """Helper to safely decrypt env vars."""
        if not value:
            return ""
        if value.startswith("gAAAA"):
            decrypted = self.security.decrypt(value)
            if decrypted is None:
                Logger.log("SYSTEM", "WARN", f"{env_name} 解密失败，已忽略该配置项")
                return ""
            return decrypted
        return value

    def initialize(self) -> bool:
        acc_id_str = self._decrypt_env("MT5_ACCOUNT_ID", os.getenv("MT5_ACCOUNT_ID"))
        pwd = self._decrypt_env("MT5_PASSWORD", os.getenv("MT5_PASSWORD"))
        srv = self._decrypt_env("MT5_SERVER", os.getenv("MT5_SERVER"))
        mt5_path = self._decrypt_env("MT5_PATH", os.getenv("MT5_PATH"))

        acc_id = int(acc_id_str) if acc_id_str and acc_id_str.isdigit() else 0

        init_params: dict[str, Any] = {}
        if mt5_path:
            init_params["path"] = mt5_path

        with self._lock:
            # 1. Initialize Terminal
            if not mt5.initialize(**init_params):
                if mt5_path and not mt5.initialize():
                    Logger.log("SYSTEM", "ERROR", f"MT5 Init Failed (Path & Default): {mt5.last_error()}")
                    return False
                elif not mt5_path:
                    Logger.log("SYSTEM", "ERROR", f"MT5 Init Failed: {mt5.last_error()}")
                    return False

            if not mt5.terminal_info():
                Logger.log("SYSTEM", "ERROR", "MT5 Initialized but Terminal Info unavailable")
                mt5.shutdown()
                return False
            
            self._connected = True

            # 2. Verify Account Mode & Login
            current_idx = mt5.account_info()
            if current_idx:
                self._log_account_mode(current_idx)

            if acc_id != 0:
                if current_idx and current_idx.login == acc_id:
                    Logger.log("SYSTEM", "INFO", f"检测到终端已登录目标账号 {acc_id}")
                else:
                    Logger.log("SYSTEM", "INFO", f"正在尝试登录账号 {acc_id}...")
                    if not mt5.login(acc_id, password=pwd, server=srv):
                        Logger.log("SYSTEM", "ERROR", f"登录失败: {mt5.last_error()} (Account: {acc_id})")
                        self.shutdown()
                        return False
            else:
                if current_idx:
                     Logger.log("SYSTEM", "WARN", f"未配置指定账号, 使用当前终端账号: {current_idx.login}")
                else:
                     Logger.log("SYSTEM", "ERROR", "未配置账号且当前终端未登录")
                     self.shutdown()
                     return False

            final_acc = mt5.account_info()
            if final_acc:
                Logger.log("SYSTEM", "INFO", f"MT5就绪. 账号:{final_acc.login} @ {final_acc.server}")
                return True
            
            return False

    def _log_account_mode(self, account_info: Any) -> None:
        mode_str = "Unknown"
        if account_info.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            mode_str = "HEDGING (对冲模式)"
        elif account_info.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING:
            mode_str = "NETTING (净额模式)"
        else:
            mode_str = f"Mode {account_info.margin_mode}"
        Logger.log("SYSTEM", "INFO", f"账户模式: {mode_str}")
        if account_info.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING:
            Logger.log("SYSTEM", "WARN", "注意: 当前策略非 HEDGING 设计，在 NETTING 模式下可能无法正确管理多层网格持仓。")

    def shutdown(self) -> None:
        with self._lock:
            if self._connected:
                mt5.shutdown()
                self._connected = False
                Logger.log("SYSTEM", "SHUTDOWN", "MT5连接已关闭")

    def ensure_symbol(self, symbol: str) -> bool:
        if symbol in self._subscribed_symbols:
            return True
        
        with self._lock:
            # Check availability first
            info = mt5.symbol_info(symbol)
            if info and info.select:
                self._subscribed_symbols.add(symbol)
                return True
            
            if mt5.symbol_select(symbol, True):
                self._subscribed_symbols.add(symbol)
                return True
            
            Logger.log("SYSTEM", "ERROR", f"Symbol select failed: {symbol}")
            return False

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